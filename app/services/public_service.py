"""
PublicQueueService — informations PUBLIQUES de la file (sans authentification).

Deux usages :
  1. Validation d'un QR code de ticket (client qui scanne son ticket, agent qui scanne le client)
  2. Écran de salle d'attente / QR d'agence : qui est appelé où, combien d'attente par service

Règle absolue : AUCUNE donnée personnelle. Ni nom, ni téléphone, ni identifiant
d'utilisateur ; uniquement des numéros de ticket, des guichets et des durées.
"""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import get_queue_size
from app.models.branch import Branch, Service
from app.models.counter import Counter
from app.models.ticket import Ticket, TicketStatus
from app.models.user import User, UserRole
from app.services.ticket_service import TicketService, as_utc, is_branch_open_now

logger = logging.getLogger(__name__)


class PublicQueueService:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_ticket_by_qr(self, qr_token: str, viewer: User | None = None) -> dict:
        """
        Valide un QR code et retourne l'état NON sensible du ticket.

        Le token est un UUID aléatoire non devinable ; il ne donne accès qu'à des
        informations de file. Un ticket de plus de QR_TICKET_TTL_HOURS est « introuvable ».

        Args:
            qr_token: Valeur du QR code
            viewer: Utilisateur connecté (optionnel). S'il est du staff de la même
                    organisation, il reçoit en plus `ticket_id` et la priorité demandée.

        Returns:
            Dict : numéro, statut, position, attente estimée, service, agence, guichet…

        Raises:
            HTTPException 404: QR inconnu ou ticket expiré
        """
        ticket = (await self.db.execute(
            select(Ticket).where(Ticket.qr_token == qr_token)
        )).scalar_one_or_none()

        expiry = datetime.now(timezone.utc) - timedelta(hours=settings.QR_TICKET_TTL_HOURS)
        if ticket is None or as_utc(ticket.created_at) < expiry:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "TICKET_NOT_FOUND", "message": "Ticket introuvable ou expiré"},
            )

        view = await TicketService(self.db)._build_response(ticket)
        service_name = (await self.db.execute(
            select(Service.name).where(Service.id == ticket.service_id)
        )).scalar_one_or_none()
        branch_name = (await self.db.execute(
            select(Branch.name).where(Branch.id == ticket.agency_id)
        )).scalar_one_or_none()

        data = {
            "number": ticket.number,
            "status": view.status,
            "priority": view.priority,
            "position": view.position,
            "total_in_queue": view.total_in_queue,
            "estimated_wait_minutes": view.estimated_wait_minutes,
            "service_name": service_name,
            "branch_name": branch_name,
            "counter_name": ticket.counter_name,
            "created_at": ticket.created_at,
        }

        if self._is_staff_of(viewer, ticket.org_id):
            data["ticket_id"] = ticket.id
            data["requested_priority"] = view.requested_priority
            data["agency_id"] = ticket.agency_id
            data["service_id"] = ticket.service_id
        return data

    async def get_branch_display(self, branch_id: str) -> dict:
        """
        Données de l'écran de salle d'attente d'une branch.

        Pour chaque guichet ouvert : le numéro en cours de traitement. Pour chaque
        service actif : nombre de personnes en attente, attente estimée pour un
        nouvel arrivant et dernier numéro appelé.

        Args:
            branch_id: Identifiant de la branch (visible dans le QR code de l'agence)

        Raises:
            HTTPException 404: Branch introuvable ou inactive
        """
        branch = (await self.db.execute(
            select(Branch).where(Branch.id == branch_id, Branch.is_active == True)  # noqa: E712
        )).scalar_one_or_none()
        if branch is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "BRANCH_NOT_FOUND", "message": "Agence introuvable"},
            )
        ticket_service = TicketService(self.db)

        counters = (await self.db.execute(
            select(Counter).where(Counter.agency_id == branch_id, Counter.is_active == True)  # noqa: E712
            .order_by(Counter.name)
        )).scalars().all()
        counter_rows = []
        for counter in counters:
            number = None
            if counter.current_ticket_id:
                number = (await self.db.execute(
                    select(Ticket.number).where(Ticket.id == counter.current_ticket_id)
                )).scalar_one_or_none()
            counter_rows.append({
                "id": counter.id,
                "name": counter.name,
                "is_open": counter.is_open,
                "now_serving": number,
            })

        services = (await self.db.execute(
            select(Service).where(Service.branch_id == branch_id, Service.is_active == True)  # noqa: E712
            .order_by(Service.ticket_prefix)
        )).scalars().all()
        service_rows = []
        for service in services:
            waiting = await self._waiting_count(branch, service)
            service_rows.append({
                "id": service.id,
                "name": service.name,
                "ticket_prefix": service.ticket_prefix,
                "waiting": waiting,
                "estimated_wait_minutes": await ticket_service.estimate_wait_minutes(service, waiting + 1),
                "last_called_number": await ticket_service._current_called_number(service.id),
            })

        return {
            "branch": {"id": branch.id, "name": branch.name, "is_open": is_branch_open_now(branch)},
            "counters": counter_rows,
            "services": service_rows,
            "generated_at": datetime.now(timezone.utc),
        }

    async def _waiting_count(self, branch: Branch, service: Service) -> int:
        """
        Personnes en attente dans un service : Redis (rapide), PostgreSQL en secours.

        Si Redis est indisponible, l'écran de salle continue de fonctionner.
        """
        try:
            return await get_queue_size(branch.org_id, branch.id, service.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Redis indisponible pour l'écran de salle ({exc}) : repli sur la base")
            return (await self.db.execute(
                select(func.count(Ticket.id)).where(
                    Ticket.service_id == service.id, Ticket.status == TicketStatus.WAITING
                )
            )).scalar() or 0

    @staticmethod
    def _is_staff_of(viewer: User | None, org_id: str) -> bool:
        """True si le visiteur est un membre du staff de l'organisation du ticket (ou super-admin)."""
        if viewer is None or viewer.role == UserRole.CLIENT:
            return False
        return viewer.role == UserRole.SUPER_ADMIN or viewer.org_id == org_id
