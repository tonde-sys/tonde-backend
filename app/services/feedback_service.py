"""
Service Feedback — envoi et consultation des retours utilisateurs.

Trois règles portent tout le reste :

1. **Quota horaire par utilisateur, pas par IP.** Un cybercafé de Bujumbura fait
   sortir des dizaines de clients derrière la même adresse : une limite par IP
   punirait des gens qui n'ont rien fait. Cinq retours par heure et par compte
   suffisent largement à un usage honnête, et bloquent un envoi en boucle.

2. **Un seul retour par ticket et par catégorie.** Sans cela, un utilisateur
   mécontent peut noyer le support en renvoyant cinquante fois le même
   signalement sur le même passage. La règle est portée par le service et non
   par un index unique : `ticket_id` est nullable, et un index unique partiel
   PostgreSQL ne protégerait que les lignes non nulles — donc pas les retours
   sur l'application elle-même, qui sont justement ceux qu'on veut pouvoir
   répéter au fil des versions.

3. **Aucune confiance dans les identifiants envoyés.** `ticket_id` doit
   appartenir à l'appelant, `org_id` doit exister. Un client modifié ne doit pas
   pouvoir rattacher son retour au passage de quelqu'un d'autre.
"""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.feedback import Feedback, FeedbackCategory, FeedbackStatus
from app.models.organization import Organization
from app.models.ticket import Ticket
from app.models.user import User
from app.schemas.feedback import CreateFeedbackRequest
from app.services import email_gateway

logger = logging.getLogger(__name__)

# Cinq retours par heure et par compte : voir la règle 1 du docstring.
HOURLY_QUOTA = 5
QUOTA_WINDOW = timedelta(hours=1)


def _serialize(feedback: Feedback) -> dict:
    """Forme publique d'un retour.

    `status` est traduit en clair pour que l'application n'ait pas à connaître
    le vocabulaire interne du support : le client lit « reçu », « lu » ou
    « traité », jamais `RECEIVED`.
    """
    return {
        "id": feedback.id,
        "category": feedback.category.value,
        "rating": feedback.rating,
        "message": feedback.message,
        "status": feedback.status.value,
        "org_id": feedback.org_id,
        "ticket_id": feedback.ticket_id,
        "app_version": feedback.app_version,
        "created_at": feedback.created_at,
        "read_at": feedback.read_at,
    }


class FeedbackService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, user: User, payload: CreateFeedbackRequest) -> dict:
        """Enregistre un retour. Lève une HTTPException explicite en cas de refus."""
        await self._enforce_hourly_quota(user.id)
        await self._reject_duplicate(user.id, payload)

        if payload.ticket_id is not None:
            await self._assert_ticket_belongs_to(user.id, payload.ticket_id)
        if payload.org_id is not None:
            await self._assert_organization_exists(payload.org_id)

        feedback = Feedback(
            user_id=user.id,
            org_id=payload.org_id,
            ticket_id=payload.ticket_id,
            category=FeedbackCategory(payload.category),
            rating=payload.rating,
            message=payload.message,
            app_version=payload.app_version,
            platform=payload.platform,
            device_model=payload.device_model,
            locale=payload.locale,
            status=FeedbackStatus.RECEIVED,
        )
        self.db.add(feedback)
        await self.db.commit()
        await self.db.refresh(feedback)

        logger.info(
            "Retour enregistré : %s (utilisateur=%s, ticket=%s)",
            feedback.category.value, user.id, feedback.ticket_id,
        )
        # Après le commit, jamais avant : si l'e-mail échoue, le retour est
        # quand même en base. L'inverse perdrait le message pour une boîte
        # SMTP momentanément fermée.
        await self._notify_support(feedback, user)
        return _serialize(feedback)

    async def list_for_user(
        self, user_id: str, page: int = 1, page_size: int = 20
    ) -> dict:
        """Historique « Mes retours », les plus récents d'abord."""
        page_size = min(max(1, page_size), 100)
        total = (
            await self.db.execute(
                select(func.count(Feedback.id)).where(Feedback.user_id == user_id)
            )
        ).scalar() or 0

        rows = (
            await self.db.execute(
                select(Feedback)
                .where(Feedback.user_id == user_id)
                .order_by(Feedback.created_at.desc())
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
        ).scalars().all()

        return {
            "items": [_serialize(f) for f in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_next": page * page_size < total,
        }

    # ── Règles internes ───────────────────────────────────────────────────────

    async def _notify_support(self, feedback: Feedback, user: User) -> None:
        """Prévient la boîte support par e-mail.

        Best-effort assumé : le retour est déjà commité. Un SMTP en panne ne
        doit pas transformer un envoi réussi en erreur 500 — le client croirait
        avoir perdu son message et le renverrait, consommant son quota.

        Sans SMTP configuré (cas du développement), on journalise : l'email
        serait de toute façon simulé par `email_gateway`.
        """
        if not email_gateway.is_configured():
            logger.info(
                "Retour %s conservé en base — SMTP non configuré, aucun e-mail envoyé",
                feedback.id,
            )
            return

        labels = {
            FeedbackCategory.REVIEW: "Avis",
            FeedbackCategory.BUG: "Problème",
            FeedbackCategory.SUGGESTION: "Suggestion",
        }
        label = labels.get(feedback.category, feedback.category.value)
        lines = [
            f"Nouveau retour depuis l'application TONDE",
            "",
            f"Catégorie   : {label}",
            f"Note        : {feedback.rating if feedback.rating is not None else 'non notée'}",
            f"Reçu le     : {feedback.created_at:%d/%m/%Y à %H:%M} (UTC)",
            "",
            "Message",
            "-------",
            feedback.message,
            "",
            "Client",
            "------",
            f"Nom         : {user.name or 'non renseigné'}",
            f"Téléphone   : {user.phone or 'non renseigné'}",
            f"E-mail      : {user.email or 'non renseigné'}",
            f"Compte      : {user.id}",
            "",
            "Contexte technique",
            "------------------",
            f"Organisation: {feedback.org_id or '—'}",
            f"Ticket      : {feedback.ticket_id or '—'}",
            f"Version app : {feedback.app_version or '—'}",
            f"Plateforme  : {feedback.platform or '—'}",
            f"Appareil    : {feedback.device_model or '—'}",
            f"Langue      : {feedback.locale or '—'}",
        ]

        try:
            await email_gateway.send_email(
                to=settings.FEEDBACK_INBOX,
                subject=f"[TONDE] {label} — {user.name or user.phone or 'client'}",
                body="\n".join(lines),
                # Répondre depuis la boîte support écrit directement au client :
                # c'est la première chose que fait quelqu'un qui traite un avis.
                reply_to=user.email,
            )
        except Exception:  # noqa: BLE001 — un retour ne doit jamais faire échouer l'envoi
            logger.exception(
                "Échec de l'e-mail de notification pour le retour %s", feedback.id
            )

    async def _enforce_hourly_quota(self, user_id: str) -> None:
        since = datetime.now(timezone.utc) - QUOTA_WINDOW
        recent = (
            await self.db.execute(
                select(func.count(Feedback.id)).where(
                    Feedback.user_id == user_id,
                    Feedback.created_at >= since,
                )
            )
        ).scalar() or 0

        if recent >= HOURLY_QUOTA:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "FEEDBACK_RATE_LIMITED",
                    "message": (
                        f"Vous avez envoyé {HOURLY_QUOTA} retours dans la dernière heure. "
                        "Réessayez plus tard."
                    ),
                },
            )

    async def _reject_duplicate(
        self, user_id: str, payload: CreateFeedbackRequest
    ) -> None:
        if payload.ticket_id is None:
            return
        existing = (
            await self.db.execute(
                select(Feedback.id).where(
                    Feedback.user_id == user_id,
                    Feedback.ticket_id == payload.ticket_id,
                    Feedback.category == FeedbackCategory(payload.category),
                )
            )
        ).first()
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "FEEDBACK_ALREADY_SENT",
                    "message": "Vous avez déjà envoyé un retour de ce type pour ce ticket.",
                },
            )

    async def _assert_ticket_belongs_to(self, user_id: str, ticket_id: str) -> None:
        owner = (
            await self.db.execute(
                select(Ticket.user_id).where(Ticket.id == ticket_id)
            )
        ).scalar_one_or_none()

        # Même réponse que le ticket n'existe pas : un client ne doit pas
        # pouvoir sonder l'existence des tickets des autres.
        if owner is None or owner != user_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "TICKET_NOT_FOUND", "message": "Ticket introuvable"},
            )

    async def _assert_organization_exists(self, org_id: str) -> None:
        exists = (
            await self.db.execute(
                select(Organization.id).where(Organization.id == org_id)
            )
        ).scalar_one_or_none()
        if exists is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "ORGANIZATION_NOT_FOUND",
                    "message": "Organisation introuvable",
                },
            )
