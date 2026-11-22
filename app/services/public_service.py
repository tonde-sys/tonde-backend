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
from app.models.organization import Organization
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
            "estimated_wait_confidence": view.estimated_wait_confidence,
            "eta_calculated_at": view.eta_calculated_at,
            "eta_version": view.eta_version,
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

    async def list_organizations(self) -> list[dict]:
        """
        Organisations actives, pour la découverte côté mobile.

        Pourquoi une route PUBLIQUE alors que `/organizations` existe : celle-ci
        est réservée aux administrateurs (`SUPER_ADMIN` / `ADMIN_ORG`) et répond
        403 à un client. Un client n'a pourtant pas besoin d'être membre pour
        prendre un ticket — seule l'agence et le service comptent. Sans cette
        route, l'application ne pouvait lister aucune organisation et l'écran de
        prise de ticket restait vide.

        Champs strictement publics : ni e-mail, ni téléphone, ni code
        d'invitation, ni statistiques d'usage.
        """
        organizations = (await self.db.execute(
            select(Organization)
            .where(Organization.is_active == True)  # noqa: E712
            .order_by(Organization.name)
        )).scalars().all()
        return [
            {
                "id": org.id,
                "name": org.name,
                "sector": org.sector,
                "city": org.city,
                "logo_url": org.logo_url,
            }
            for org in organizations
        ]

    async def list_branches(self, org_id: str) -> list[dict]:
        """
        Agences actives d'une organisation, avec le monde en attente.

        `waiting_count` est lu en BASE et non dans Redis : une agence dont la
        file Redis est indisponible doit rester visible dans la liste (avec un
        compte peut-être décalé) plutôt que disparaître — un client qui ne voit
        plus son agence croit qu'elle a fermé.

        Une seule requête groupée pour toutes les agences : la liste est
        publique et appelée à chaque ouverture de l'écran de découverte.

        Raises:
            HTTPException 404: Organisation inexistante ou désactivée.
        """
        org = (await self.db.execute(
            select(Organization).where(
                Organization.id == org_id,
                Organization.is_active == True,  # noqa: E712
            )
        )).scalar_one_or_none()
        if org is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "ORGANIZATION_NOT_FOUND", "message": "Organisation introuvable"},
            )

        branches = (await self.db.execute(
            select(Branch)
            .where(Branch.org_id == org_id, Branch.is_active == True)  # noqa: E712
            .order_by(Branch.name)
        )).scalars().all()

        waiting: dict[str, int] = {}
        if branches:
            rows = (await self.db.execute(
                select(Ticket.agency_id, func.count(Ticket.id))
                .where(
                    Ticket.agency_id.in_([branch.id for branch in branches]),
                    Ticket.status == TicketStatus.WAITING,
                )
                .group_by(Ticket.agency_id)
            )).all()
            waiting = {agency_id: count for agency_id, count in rows}

        return [
            {
                "id": branch.id,
                "name": branch.name,
                "address": branch.address,
                "city": branch.city,
                "sector": branch.sector,
                "latitude": branch.latitude,
                "longitude": branch.longitude,
                "opens_at": branch.opens_at,
                "closes_at": branch.closes_at,
                # Calculé (horaires + interrupteur manuel), pas la colonne brute :
                # une agence peut être « ouverte » à 22 h si l'admin l'a forcée.
                "is_open": is_branch_open_now(branch),
                "waiting_count": waiting.get(branch.id, 0),
            }
            for branch in branches
        ]

    async def list_services(self, branch_id: str) -> list[dict]:
        """
        Services actifs d'une agence, avec le monde en attente par service.

        Enrichit la route publique existante plutôt que d'en créer une seconde :
        `ServiceService.list` ne renvoie que les colonnes du service, et le
        mobile affiche « N personnes devant vous » dès l'écran de choix. Sans ce
        compte, il annoncerait zéro personne à un client qui en a douze devant
        lui, ou devrait appeler la file complète pour un simple entier.

        Le compte est lu en base et non dans Redis, pour la même raison que dans
        `list_branches` : un service ne doit pas disparaître de la liste quand la
        file est momentanément indisponible.
        """
        services = (await self.db.execute(
            select(Service)
            .where(Service.branch_id == branch_id, Service.is_active == True)  # noqa: E712
            .order_by(Service.ticket_prefix)
        )).scalars().all()

        waiting: dict[str, int] = {}
        if services:
            rows = (await self.db.execute(
                select(Ticket.service_id, func.count(Ticket.id))
                .where(
                    Ticket.service_id.in_([service.id for service in services]),
                    Ticket.status == TicketStatus.WAITING,
                )
                .group_by(Ticket.service_id)
            )).all()
            waiting = {service_id: count for service_id, count in rows}

        return [
            {
                "id": service.id,
                "org_id": service.org_id,
                "branch_id": service.branch_id,
                "name": service.name,
                "description": service.description,
                "ticket_prefix": service.ticket_prefix,
                "avg_duration_minutes": service.avg_duration_minutes,
                "is_active": service.is_active,
                # Conservé tel quel : cette route remplace `ServiceService.list`,
                # qui exposait déjà `created_at`. Le retirer casserait un
                # consommateur existant pour un champ que personne ne lit ici.
                "created_at": service.created_at.isoformat() if service.created_at else None,
                "waiting_count": waiting.get(service.id, 0),
            }
            for service in services
        ]

    async def get_branch_display(self, branch_id: str) -> dict:
        """
        Données de l'écran de salle d'attente d'une branch.

        Pour chaque guichet ouvert : le numéro en cours de traitement. Pour chaque
        service actif : nombre de personnes en attente, attente estimée pour un
        nouvel arrivant et dernier numéro appelé.

        Coût : un nombre CONSTANT de requêtes, quel que soit le nombre de guichets
        et de services. Cet endpoint est public et interrogé toutes les 7 s par
        chaque poste de l'agence : la version précédente faisait une requête par
        guichet et trois par service (≈ 30 requêtes pour 12 guichets et 6 services,
        soit ~85 requêtes/s pour une seule agence). Les numéros en cours, les
        derniers appelés et les durées moyennes sont désormais lus en une requête
        groupée chacun.

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
            select(Counter).where(
                Counter.org_id == branch.org_id,
                Counter.agency_id == branch_id,
                Counter.is_active == True,  # noqa: E712
            )
            .order_by(Counter.name)
        )).scalars().all()

        # Une seule requête pour tous les numéros en cours, au lieu d'une par guichet.
        serving_numbers = await self._counter_numbers(counters, branch.org_id)
        counter_rows = [
            {
                "id": counter.id,
                "name": counter.name,
                "is_open": counter.is_open,
                "is_paused": counter.is_paused,
                "paused_until": (
                    counter.paused_until.isoformat()
                    if counter.is_paused and counter.paused_until else None
                ),
                "now_serving": serving_numbers.get(counter.current_ticket_id or ""),
            }
            for counter in counters
        ]

        services = (await self.db.execute(
            select(Service).where(
                Service.org_id == branch.org_id,
                Service.branch_id == branch_id,
                Service.is_active == True,  # noqa: E712
            )
            .order_by(Service.ticket_prefix)
        )).scalars().all()

        service_ids = [service.id for service in services]
        waiting_counts = await self._waiting_counts(branch, services)
        try:
            dynamic_estimates = await ticket_service.estimate_service_queues(list(services))
        except Exception as exc:  # noqa: BLE001 — l'écran public garde son ETA de repli
            logger.warning(f"AWTE indisponible pour l'agence {branch_id} ({exc})")
            dynamic_estimates = {}
        last_called = await ticket_service.current_called_numbers(service_ids, branch.org_id)

        service_rows = [
            {
                "id": service.id,
                "name": service.name,
                "ticket_prefix": service.ticket_prefix,
                "waiting": waiting_counts.get(service.id, 0),
                "estimated_wait_minutes": (
                    dynamic_estimates[service.id].next_ticket_minutes
                    if service.id in dynamic_estimates
                    else None
                ),
                "estimated_wait_confidence": (
                    dynamic_estimates[service.id].next_ticket.confidence
                    if service.id in dynamic_estimates else "unavailable"
                ),
                "eta_calculated_at": (
                    dynamic_estimates[service.id].calculated_at.isoformat()
                    if service.id in dynamic_estimates and dynamic_estimates[service.id].calculated_at
                    else None
                ),
                "eta_version": dynamic_estimates[service.id].version if service.id in dynamic_estimates else None,
                "last_called_number": last_called.get(service.id),
            }
            for service in services
        ]

        return {
            "branch": {"id": branch.id, "name": branch.name, "is_open": is_branch_open_now(branch)},
            "counters": counter_rows,
            "services": service_rows,
            "generated_at": datetime.now(timezone.utc),
        }

    async def _counter_numbers(self, counters: list[Counter], org_id: str | None) -> dict[str, str]:
        """
        Numéros en cours de traitement, pour tous les guichets en une requête.

        `Counter.current_ticket_id` peut pointer sur un ticket supprimé ou déjà
        terminé : on ne renvoie que les tickets réellement présents en base.
        """
        ticket_ids = [c.current_ticket_id for c in counters if c.current_ticket_id]
        if not ticket_ids:
            return {}
        rows = (await self.db.execute(
            select(Ticket.id, Ticket.number).where(
                Ticket.org_id == org_id,
                Ticket.id.in_(ticket_ids),
            )
        )).all()
        return {ticket_id: number for ticket_id, number in rows}

    async def _waiting_counts(self, branch: Branch, services: list[Service]) -> dict[str, int]:
        """
        Personnes en attente par service : Redis (rapide), PostgreSQL en secours.

        Si Redis est indisponible, l'écran de salle continue de fonctionner — et le
        repli est UNE requête groupée, pas une par service : une panne Redis ne doit
        pas se transformer en N+1 sur PostgreSQL.
        """
        if not services:
            return {}
        try:
            return {
                service.id: await get_queue_size(branch.org_id, branch.id, service.id)
                for service in services
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Redis indisponible pour l'écran de salle ({exc}) : repli sur la base")
            rows = (await self.db.execute(
                select(Ticket.service_id, func.count(Ticket.id))
                .where(
                    Ticket.service_id.in_([service.id for service in services]),
                    Ticket.org_id == branch.org_id,
                    Ticket.status == TicketStatus.WAITING,
                )
                .group_by(Ticket.service_id)
            )).all()
            counts = {service_id: count for service_id, count in rows}
            return {service.id: counts.get(service.id, 0) for service in services}

    @staticmethod
    def _is_staff_of(viewer: User | None, org_id: str) -> bool:
        """True si le visiteur est un membre du staff de l'organisation du ticket (ou super-admin)."""
        if viewer is None or viewer.role == UserRole.CLIENT:
            return False
        return viewer.role == UserRole.SUPER_ADMIN or viewer.org_id == org_id
