"""
Modèle EventOutbox — table `event_outbox`.

Spécification Queue Engine v1.0, §6.3.

Le problème qu'elle résout : `publish_event` diffuse après le `commit()` et
avale ses erreurs. Si Redis tombe exactement entre les deux, la transition est
sûre en base mais l'écran de salle ne voit jamais l'appel — il attend le prochain
sondage, ou ne l'apprend qu'à la reconnexion.

Une table Outbox écrit l'événement dans la MÊME transaction que le changement
d'état. Il est donc impossible d'avoir l'un sans l'autre :
  - transaction validée  → l'événement est en base, et sera publié (maintenant,
    ou par le prochain passage du répartiteur) ;
  - transaction annulée  → aucune trace, rien à publier.

Le compromis est celui de toute Outbox : livraison AU MOINS une fois. Un
événement peut être publié deux fois si le processus meurt entre la diffusion et
la mise à jour de `published_at`. Les consommateurs le tolèrent déjà : ils
relisent l'état courant au lieu de l'accumuler.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import String, DateTime, Integer, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.core.database import Base


class EventOutbox(Base):
    __tablename__ = "event_outbox"
    __table_args__ = (
        # Prédicat exact du répartiteur : « les non publiés, dans l'ordre d'écriture ».
        Index("ix_event_outbox_pending", "published_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Organisation destinataire : c'est la clé du canal Pub/Sub.
    # `None` = canal public (écrans de salle non authentifiés).
    org_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    event_type: Mapped[str] = mapped_column(String(50))

    # Le message tel qu'il sera diffusé. Stocké en JSON et non reconstruit au
    # rejeu : si le format évolue, un événement ancien doit être publié dans la
    # forme où il a été produit, pas dans la forme du jour.
    payload: Mapped[dict] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # NULL = en attente. C'est la seule colonne que le répartiteur modifie.
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Un événement qui échoue en boucle ne doit pas être réessayé sans fin :
    # `attempts` permet de le sortir du circuit et de le signaler.
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
