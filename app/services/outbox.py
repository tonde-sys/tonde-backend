"""
Outbox transactionnelle — spécification Queue Engine v1.0, §6.3.

`publish_event` diffuse en temps réel et ne lève jamais. C'est le bon choix pour
le métier (une panne de Pub/Sub ne doit pas annuler un appel de ticket), mais il
laisse un trou : si Redis tombe entre le `commit()` et la diffusion, l'événement
est perdu. L'état est correct en base, l'écran de salle ne l'a jamais su.

Ce module comble le trou sans rendre le métier dépendant de Redis :

  1. `enqueue` écrit l'événement dans `event_outbox` **dans la transaction
     métier** — donc l'écriture et le changement d'état réussissent ou échouent
     ensemble ;
  2. la diffusion immédiate reste tentée (latence inchangée) ; si elle réussit,
     la ligne est marquée publiée dans la même transaction ;
  3. si elle échoue, la ligne reste en attente et `dispatch` la reprend au
     passage suivant du scheduler.

Le contrat est « au moins une fois » : un événement peut être diffusé deux fois
si le processus meurt entre la diffusion et la mise à jour. Les consommateurs le
tolèrent (ils relisent l'état), et c'est le prix standard d'une Outbox.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event_outbox import EventOutbox
from app.websocket.publisher import publish_event

logger = logging.getLogger(__name__)

# Au-delà, l'événement est considéré comme irrécupérable et sort du circuit.
# Il reste en base (traçable) au lieu d'être supprimé : on veut pouvoir
# répondre à « pourquoi cet écran n'a-t-il rien reçu ? ».
MAX_ATTEMPTS = 5

# Un événement plus vieux que cela n'a plus d'intérêt : l'écran s'est
# resynchronisé dix fois depuis. Le publier ne ferait qu'ajouter du bruit.
MAX_AGE_MINUTES = 60


class OutboxService:
    """Écriture et rejeu des événements temps réel."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def enqueue(
        self,
        *,
        org_id: str | None,
        event_type: str,
        payload: dict,
    ) -> str:
        """
        Écrit l'événement dans la transaction courante. NE COMMIT PAS.

        C'est délibéré : l'appelant commite le changement d'état et l'événement
        ensemble, ce qui est toute la garantie apportée par l'Outbox. Commiter
        ici romprait cette atomicité.

        Returns:
            L'identifiant de la ligne, à passer à `mark_published`.
        """
        row = EventOutbox(org_id=org_id, event_type=event_type, payload=payload)
        self.db.add(row)
        await self.db.flush()
        return row.id

    async def mark_published(self, row_id: str) -> None:
        """Marque la ligne comme diffusée. NE COMMIT PAS (même transaction)."""
        row = await self.db.get(EventOutbox, row_id)
        if row is not None:
            row.published_at = datetime.now(timezone.utc)

    async def publish_now(self, row_id: str, org_id: str | None, payload: dict) -> bool:
        """
        Tente la diffusion immédiate et marque la ligne si elle réussit.

        `publish_event` ne lève jamais : il retourne False. On s'appuie dessus
        plutôt que sur une exception, car c'est le contrat réel du module de
        diffusion (et c'est ce que les tests de panne simulent).
        """
        published = await publish_event(org_id, payload)
        if published:
            await self.mark_published(row_id)
        return published

    async def dispatch(self, limit: int = 100) -> int:
        """
        Republie les événements en attente. Retourne le nombre de lignes traitées.

        Appelé par le scheduler. Ne lève pas : un rejeu raté ne doit pas arrêter
        la maintenance des files, qui est plus critique.

        Une ligne en échec voit son compteur augmenter ; au-delà de
        `MAX_ATTEMPTS` elle est marquée comme publiée avec l'erreur en clair.
        Elle sort alors du circuit sans disparaître — un événement perdu doit
        rester constatable.
        """
        now = datetime.now(timezone.utc)
        rows = (await self.db.execute(
            select(EventOutbox)
            .where(
                EventOutbox.published_at.is_(None),
                EventOutbox.attempts < MAX_ATTEMPTS,
                EventOutbox.created_at >= now - timedelta(minutes=MAX_AGE_MINUTES),
            )
            .order_by(EventOutbox.created_at)
            .limit(limit)
        )).scalars().all()

        if not rows:
            return 0

        traites = 0
        for row in rows:
            traites += 1
            try:
                published = await publish_event(row.org_id, row.payload)
            except Exception as exc:  # noqa: BLE001 — publish_event ne lève pas, ceci est un filet
                published = False
                row.last_error = str(exc)[:500]

            if published:
                row.published_at = now
                continue

            row.attempts += 1
            row.last_error = row.last_error or "Redis indisponible"
            if row.attempts >= MAX_ATTEMPTS:
                # On abandonne la diffusion mais on GARDE la trace : la ligne
                # reste en base avec son erreur, pour l'exploitation.
                row.published_at = now
                logger.error(
                    f"Outbox : événement {row.event_type} abandonné après "
                    f"{row.attempts} tentatives ({row.last_error})"
                )

        try:
            await self.db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Outbox : mise à jour des états impossible : {exc}")
            await self.db.rollback()
            return 0
        return traites
