"""
Service Tickets — Queue Engine de TONDE.

Responsabilités :
  - Création et suivi des tickets (idempotent : client_action_id)
  - Machine à états explicite (transitions contrôlées)
  - Appel atomique du prochain ticket (ZPOPMIN + compare-and-set en base)
  - Calcul ETA (moyenne glissante des derniers services réels)
  - Intégration Redis (file d'attente, position, échéances) avec auto-réparation
    depuis PostgreSQL (la vérité) si Redis est vide ou désynchronisé
  - Isolation multi-tenant via org_id + périmètre par guichet / branche pour le staff
  - Transfert, confirmation de priorité, clôtures et expirations automatiques
  - Audit trail via QueueLog / AuditLog (source de données Analytics)
  - Publication d'événements temps réel (jamais bloquante)

Toute modification de ce fichier doit être analysée
avec soin : c'est le cœur du système.
"""
import json
import logging
import math
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import ORG_WIDE_ROLES, forbidden, get_staff_branch_ids
from app.core.config import settings
from app.core.redis import (
    DEADLINE_ABSENT,
    DEADLINE_WARNING,
    acquire_ticket_creation_lock,
    add_to_queue,
    add_to_queue_if_absent,
    clear_deadlines,
    get_queue_position,
    get_queue_size,
    get_queue_snapshot,
    mark_once,
    next_ticket_sequence,
    pop_next_ticket,
    queue_score,
    release_ticket_creation_lock,
    remove_from_queue,
    restore_to_queue,
    set_deadline,
)
from app.models.audit_log import AuditAction
from app.models.branch import Branch, Service
from app.models.counter import Counter
from app.models.employee import Employee, EmployeeStatus
from app.models.queue_log import QueueLog, QueueLogAction
from app.models.ticket import (
    ALLOWED_TRANSITIONS,
    PRIORITY_SCORES,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.models.user import User, UserRole
from app.schemas.ticket import CreateTicketRequest, TicketHistoryResponse, TicketResponse
from app.services.audit_service import AuditService
from app.websocket.events import EventType
from app.websocket.publisher import publish_event, queue_snapshot_event

logger = logging.getLogger(__name__)

# Statuts considérés comme "actifs" — bloquent la création d'un nouveau ticket.
# Règle MVP DÉCISION 1 : 1 seul ticket actif par utilisateur sur TOUTE la plateforme.
ACTIVE_STATUSES: list[TicketStatus] = [
    TicketStatus.WAITING,
    TicketStatus.CALLED,
    TicketStatus.SERVING,
    TicketStatus.ABSENT,
    TicketStatus.TRANSFERRED,
    TicketStatus.INCOMPLETE,
]

# Mapping status → QueueLogAction pour l'audit trail
STATUS_TO_LOG_ACTION: dict[TicketStatus, QueueLogAction] = {
    TicketStatus.CALLED:      QueueLogAction.TICKET_CALLED,
    TicketStatus.SERVING:     QueueLogAction.TICKET_SERVING,
    TicketStatus.DONE:        QueueLogAction.TICKET_DONE,
    TicketStatus.ABSENT:      QueueLogAction.TICKET_ABSENT,
    TicketStatus.WAITING:     QueueLogAction.TICKET_RETURNED,   # retour ABSENT/INCOMPLETE/TRANSFERRED → WAITING
    TicketStatus.TRANSFERRED: QueueLogAction.TICKET_TRANSFERRED,
    TicketStatus.CANCELLED:   QueueLogAction.TICKET_CANCELLED,
    TicketStatus.INCOMPLETE:  QueueLogAction.TICKET_INCOMPLETE,
}

# Burundi = Africa/Bujumbura = UTC+2, sans heure d'été.
# La séquence de tickets repart à 1 à minuit HEURE LOCALE (pas minuit UTC).
BURUNDI_TZ = timezone(timedelta(hours=2))

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# Nombre maximum d'entrées périmées sautées par appel (ticket annulé/supprimé
# mais resté dans la file Redis). Évite une boucle infinie sur une file corrompue.
MAX_STALE_QUEUE_ENTRIES = 25

# ETA : nombre de services récents moyennés, et poids du défaut quand l'historique est court
ETA_SAMPLE_SIZE = 10
ETA_MIN_SERVICE_SECONDS = 30
ETA_MAX_SERVICE_SECONDS = 3600

# Rôles autorisés à appeler depuis n'importe quel guichet de leur organisation.
# Un AGENT, lui, ne peut appeler que depuis le guichet qui lui est affecté.
ANY_COUNTER_ROLES: set[UserRole] = {
    UserRole.SUPERVISOR,
    UserRole.ADMIN_AGENCY,
    UserRole.ADMIN_ORG,
    UserRole.SUPER_ADMIN,
}


# ══ Fonctions utilitaires pures ═══════════════════════════════════════════════

def as_utc(value: datetime) -> datetime:
    """Rend une date « aware » en UTC (SQLite renvoie des dates naïves : on les suppose UTC)."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def to_epoch_ms(value: datetime) -> int:
    """Convertit une date en millisecondes depuis l'epoch (base du score Redis)."""
    return int(as_utc(value).timestamp() * 1000)


def is_branch_open_now(branch: Branch, now_local: datetime | None = None) -> bool:
    """
    Indique si une branch accepte des tickets à cet instant.

    Conditions : branch active ET ouverte (interrupteur manuel) ET, si des horaires
    détaillés sont configurés (`operating_hours`), instant compris dans le créneau
    du jour. Sans `operating_hours`, seul l'interrupteur manuel compte.

    Args:
        branch: La branch à tester
        now_local: Instant à tester, en heure du Burundi (défaut : maintenant)
    """
    if not (branch.is_active and branch.is_open):
        return False
    hours = branch.operating_hours
    if not hours:
        return True
    now_local = now_local or datetime.now(BURUNDI_TZ)
    slot = hours.get(_WEEKDAYS[now_local.weekday()])
    if not slot:
        return False
    return slot["open"] <= now_local.strftime("%H:%M") <= slot["close"]


class TicketService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ══ Création ═══════════════════════════════════════════════════════════════

    async def create_ticket(
        self, data: CreateTicketRequest, user_id: str, org_id: str | None
    ) -> TicketResponse:
        """
        Crée un ticket et l'insère dans la file d'attente Redis.

        Idempotent : si `client_action_id` est fourni et qu'un ticket existe déjà
        pour (user, client_action_id), ce ticket est retourné sans en créer un second.

        Priorité : un client ne s'attribue jamais une priorité. Le ticket est créé en
        « standard » ; la priorité demandée est conservée dans `requested_priority` et
        un agent l'approuve ou la rejette (decide_priority).

        Args:
            data: Données de création (agency_id, service_id, priority, client_action_id)
            user_id: ID de l'utilisateur connecté
            org_id: ID de l'organisation (isolation multi-tenant)
                    Peut être None pour les clients OTP sans affiliation

        Returns:
            TicketResponse avec numéro, position, ETA et QR token

        Raises:
            HTTPException 400: Agence fermée, hors horaires, ou service inactif
            HTTPException 404: Agence ou service introuvable
            HTTPException 409: Ticket actif déjà existant, quota journalier atteint, ou
                               création déjà en cours pour cet utilisateur (double envoi)
            HTTPException 503: File d'attente indisponible (le ticket est annulé proprement)
        """
        # Verrou court par utilisateur : deux requêtes simultanées (double tap,
        # retry réseau) ne peuvent pas passer toutes deux le contrôle
        # « 1 seul ticket actif » avant que l'une ait créé son ticket.
        if not await acquire_ticket_creation_lock(user_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "TICKET_CREATION_IN_PROGRESS",
                    "message": (
                        "Une création de ticket est déjà en cours pour votre compte. "
                        "Patientez un instant puis consultez votre ticket."
                    ),
                },
            )
        try:
            return await self._create_ticket(data, user_id, org_id)
        finally:
            await release_ticket_creation_lock(user_id)

    async def _create_ticket(
        self, data: CreateTicketRequest, user_id: str, org_id: str | None
    ) -> TicketResponse:
        """Logique de création (appelée sous verrou par create_ticket)."""
        # ── Idempotence : même action déjà traitée → on renvoie son résultat ──
        if data.client_action_id:
            replay = await self._find_by_client_action(user_id, data.client_action_id)
            if replay is not None:
                logger.info(f"Création rejouée (idempotence): {replay.number} | user={user_id}")
                return await self._build_response(replay)

        # Récupérer la branch — si org_id est None, on cherche globalement
        # puis on utilise son org_id pour le reste
        branch = await self._get_agency_for_creation(data.agency_id, org_id)

        # Si le client n'a pas d'org, on utilise l'org de la branch
        ticket_org_id = org_id if org_id else branch.org_id

        if not is_branch_open_now(branch):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "BRANCH_CLOSED", "message": "Cette branch est fermée en ce moment"},
            )

        # Vérifier que le service existe dans cette agence et qu'il est actif
        service = await self._get_service(data.service_id, data.agency_id, ticket_org_id)
        if not service.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "SERVICE_INACTIVE", "message": "Ce service n'est pas disponible"},
            )

        # Règle DÉCISION 1 : 1 seul ticket actif par utilisateur sur toute la plateforme
        existing = await self._get_active_ticket(user_id)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "TICKET_ALREADY_ACTIVE",
                    "message": (
                        f"Vous avez déjà un ticket actif ({existing.number} — "
                        f"statut : {existing.status.value}). "
                        "Attendez qu'il soit terminé ou annulez-le avant d'en prendre un nouveau."
                    ),
                    "active_ticket_id": existing.id,
                    "active_ticket_number": existing.number,
                },
            )

        # Quota journalier de la branch (Branch.max_daily_tickets)
        await self._enforce_daily_limit(branch)

        # Calculer le prochain numéro de séquence (réinitialisé chaque jour)
        sequence = await self._get_next_sequence(data.agency_id, service.ticket_prefix)
        ticket_number = f"{service.ticket_prefix}-{sequence:03d}"

        # Priorité : le ticket démarre TOUJOURS en standard ; la demande attend un agent
        requested = TicketPriority(data.priority)
        requested_priority = requested if requested != TicketPriority.STANDARD else None

        ticket = Ticket(
            number=ticket_number,
            prefix=service.ticket_prefix,
            sequence=sequence,
            user_id=user_id,
            org_id=ticket_org_id,
            agency_id=data.agency_id,
            service_id=data.service_id,
            priority=TicketPriority.STANDARD,
            requested_priority=requested_priority,
            client_action_id=data.client_action_id,
            status=TicketStatus.WAITING,
        )
        self.db.add(ticket)
        try:
            await self.db.flush()   # obtenir ticket.id avant commit
            await self.db.commit()
        except IntegrityError:
            # Course inter-instances sur (user_id, client_action_id) : le gagnant a déjà créé
            await self.db.rollback()
            if data.client_action_id:
                replay = await self._find_by_client_action(user_id, data.client_action_id)
                if replay is not None:
                    return await self._build_response(replay)
            raise
        await self.db.refresh(ticket)

        # ── Audit trail : log la création ─────────────────────────────────────
        await self._log_action(
            ticket=ticket,
            action=QueueLogAction.TICKET_CREATED,
            from_status=None,
            to_status=TicketStatus.WAITING,
            actor_id=user_id,
            actor_role=UserRole.CLIENT.value,
            elapsed_seconds=0,
        )
        await self.db.commit()

        # Insérer dans la file Redis — segmentée par service (DÉCISION 5).
        # Si Redis échoue, on annule proprement le ticket : sinon il resterait WAITING
        # en base sans être en file, et bloquerait le client (règle « 1 ticket actif »).
        try:
            position = await add_to_queue(
                ticket.org_id, ticket.agency_id, ticket.service_id, ticket.id,
                PRIORITY_SCORES[TicketPriority.STANDARD],
                created_ms=to_epoch_ms(ticket.created_at),
            )
            total = await get_queue_size(ticket.org_id, ticket.agency_id, ticket.service_id)
        except Exception as exc:
            logger.error(f"Redis indisponible à la création du ticket {ticket.number}: {exc}")
            await self._transition(
                ticket, TicketStatus.CANCELLED, actor_role="system",
                extra={"reason": "queue_unavailable"},
            )
            await self.db.commit()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "QUEUE_UNAVAILABLE",
                    "message": "La file d'attente est momentanément indisponible. Réessayez.",
                },
            ) from exc

        eta = await self.estimate_wait_minutes(service, position)
        ticket.estimated_wait_minutes = eta
        await self.db.commit()

        logger.info(
            f"Ticket créé: {ticket_number} | org={ticket.org_id} | position={position} | ETA={eta}min"
            + (f" | priorité demandée={requested_priority.value}" if requested_priority else "")
        )
        return self._to_response(ticket, position=position, total=total, eta=eta)

    # ══ Consultation ═══════════════════════════════════════════════════════════

    async def get_ticket(
        self,
        ticket_id: str,
        user_id: str,
        org_id: str | None,
        actor_role: UserRole | None = None,
    ) -> TicketResponse:
        """
        Retourne les informations actuelles d'un ticket.

        Sécurité : un client ne peut voir que ses propres tickets.
        org_id garantit l'isolation multi-tenant.

        Auto-réparation : si le ticket est WAITING mais absent de Redis (Redis vidé ou
        redémarré), il est remis en file à sa place d'origine avant de calculer sa position.

        Args:
            ticket_id: ID du ticket
            user_id: ID de l'utilisateur connecté
            org_id: Organisation de l'utilisateur. None pour un client OTP
                    (sans organisation) : le ticket est alors retrouvé par
                    propriétaire, et l'org du ticket sert pour la suite.

        Raises:
            HTTPException 403: Le ticket appartient à un autre utilisateur
            HTTPException 404: Ticket introuvable
        """
        if org_id is None:
            ticket = await self._get_ticket_for_owner(ticket_id, user_id)
        else:
            ticket = await self._get_ticket_by_id(ticket_id, org_id)

        # Le contrôle de propriété ne s'applique QU'AUX clients.
        # Un agent/superviseur/admin consulte les tickets de son organisation :
        # sans cette exception, un guichetier ne pouvait pas ouvrir le ticket
        # qu'il venait lui-même d'appeler (403 systématique), ce qui bloquait
        # tout l'enchaînement /serving → /done côté desktop.
        is_staff = actor_role is not None and actor_role != UserRole.CLIENT
        if not is_staff and ticket.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "FORBIDDEN", "message": "Accès non autorisé à ce ticket"},
            )
        return await self._build_response(ticket)

    async def get_ws_snapshot(self, ticket_id: str, user_id: str) -> dict | None:
        """
        État courant d'un ticket au format d'un événement `queue_update`.

        Envoyé au client à chaque (re)connexion WebSocket : le WebSocket ne rejoue pas
        l'historique, ce snapshot lui donne immédiatement son état réel.

        Returns:
            Dict prêt à sérialiser, ou None si le ticket n'existe pas / n'appartient pas à l'user.
        """
        try:
            ticket = await self._get_ticket_for_owner(ticket_id, user_id)
        except HTTPException:
            return None
        response = await self._build_response(ticket)
        return {
            "type": EventType.QUEUE_UPDATE.value,
            "ticket_id": ticket.id,
            "ticket_number": ticket.number,
            "status": response.status,
            "position": response.position,
            "total_in_queue": response.total_in_queue,
            "estimated_wait_minutes": response.estimated_wait_minutes or 0,
            "current_called_number": await self._current_called_number(ticket.service_id) or "",
            "counter_name": ticket.counter_name,
        }

    async def get_history(
        self, user_id: str, page: int = 1
    ) -> TicketHistoryResponse:
        """
        Retourne l'historique paginé des tickets d'un utilisateur.

        Args:
            user_id: ID de l'utilisateur
            page: Numéro de page (commence à 1)

        Returns:
            TicketHistoryResponse avec items, total, page, has_next
        """
        page_size = 20
        offset = (page - 1) * page_size

        count_result = await self.db.execute(
            select(func.count(Ticket.id)).where(Ticket.user_id == user_id)
        )
        total = count_result.scalar() or 0

        result = await self.db.execute(
            select(Ticket)
            .where(Ticket.user_id == user_id)
            .order_by(Ticket.created_at.desc())
            .limit(page_size)
            .offset(offset)
        )
        tickets = result.scalars().all()

        items = [
            self._to_response(t, position=0, total=0, eta=t.estimated_wait_minutes)
            for t in tickets
        ]

        return TicketHistoryResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            has_next=(offset + page_size) < total,
        )

    # ══ Actions du client ══════════════════════════════════════════════════════

    async def cancel_ticket(
        self, ticket_id: str, user_id: str, org_id: str | None
    ) -> dict:
        """
        Le client annule son ticket.
        Seuls les tickets WAITING peuvent être annulés par le client.

        Args:
            ticket_id: ID du ticket à annuler
            user_id: ID de l'utilisateur connecté (doit être le propriétaire)
            org_id: Organisation de l'utilisateur. None pour un client OTP :
                    l'org du ticket est alors utilisée pour la file Redis.

        Raises:
            HTTPException 400: Transition interdite (ticket non WAITING)
            HTTPException 403: Le ticket appartient à un autre utilisateur
            HTTPException 404: Ticket introuvable
        """
        if org_id is None:
            ticket = await self._get_ticket_for_owner(ticket_id, user_id)
        else:
            ticket = await self._get_ticket_by_id(ticket_id, org_id)

        if ticket.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "FORBIDDEN", "message": "Accès non autorisé"},
            )

        await self._transition(
            ticket, TicketStatus.CANCELLED,
            actor_id=user_id, actor_role=UserRole.CLIENT.value,
        )
        await self.db.commit()
        await self._leave_queue(ticket)

        logger.info(f"Ticket annulé: {ticket.number} | user={user_id}")
        return {"success": True, "message": "Ticket annulé"}

    async def return_to_queue(
        self,
        ticket_id: str,
        org_id: str,
        user_id: str | None = None,
        actor: User | None = None,
    ) -> dict:
        """
        Un ticket ABSENT revient dans la file : ABSENT → WAITING.
        Le ticket revient en fin de file, en priorité standard (pénalité).

        Deux appelants possibles :
          - le CLIENT (user_id fourni) : ticket retrouvé par propriétaire ;
          - le STAFF (actor fourni) : ticket de sa branch uniquement.

        Args:
            ticket_id: ID du ticket absent
            org_id: Organisation de l'appelant (staff)
            user_id: Client OTP sans organisation : un client ne remet en file que son ticket
            actor: Agent/superviseur/admin qui remet le ticket en file

        Raises:
            HTTPException 400: Transition interdite (ticket non ABSENT)
            HTTPException 403: Staff hors de sa branch
            HTTPException 404: Ticket introuvable (ou appartenant à un autre user)
        """
        if user_id is not None:
            ticket = await self._get_ticket_for_owner(ticket_id, user_id)
        elif actor is not None:
            ticket = await self._get_ticket_for_staff(ticket_id, actor)
            await self._assert_staff_in_branch(actor, ticket)
        else:
            ticket = await self._get_ticket_by_id(ticket_id, org_id)

        await self._transition(
            ticket, TicketStatus.WAITING,
            actor_id=user_id or (actor.id if actor else None),
            actor_role=UserRole.CLIENT.value if user_id else (actor.role.value if actor else None),
        )
        # Pénalité : retour en fin de file, priorité standard
        ticket.priority = TicketPriority.STANDARD
        await self.db.commit()

        await add_to_queue(
            ticket.org_id, ticket.agency_id, ticket.service_id, ticket.id,
            PRIORITY_SCORES[TicketPriority.STANDARD],
        )
        await self._publish_queue_snapshot(ticket.org_id, ticket.agency_id, ticket.service_id)
        return {"success": True, "ticket_id": ticket.id}

    # ══ Actions du guichetier ══════════════════════════════════════════════════

    async def call_next(
        self,
        agency_id: str,
        service_id: str,
        counter_id: str,
        org_id: str,
        actor: User,
    ) -> dict:
        """
        Le guichetier appelle le prochain ticket de la file du service.

        Garanties :
          - Atomicité Redis : le ticket est retiré de la file par une seule commande
            (ZPOPMIN). Deux guichets simultanés ne peuvent pas obtenir le même ticket.
          - Atomicité PostgreSQL : le passage WAITING → CALLED est un compare-and-set
            (UPDATE … WHERE status = 'waiting'). Même si Redis et la base se
            désynchronisent, un ticket ne peut jamais être appelé deux fois.
          - Cohérence : si l'écriture échoue, le ticket est remis dans la file à sa
            place d'origine.
          - Auto-réparation : file Redis vide alors que la base a des tickets WAITING
            → la file est reconstruite depuis PostgreSQL puis l'appel est retenté.
          - Contrôle du guichet : il doit exister dans l'org et la branch, être actif
            et ouvert ; un AGENT doit y être affecté. Le nom du guichet vient de la
            base, jamais du client.
          - Échéances : l'avertissement et le passage automatique en ABSENT sont
            programmés (ABSENT_WARNING_SECONDS / ABSENT_TIMEOUT_SECONDS).

        Args:
            agency_id: ID de l'agence du guichetier
            service_id: ID du service dont appeler le prochain ticket (DÉCISION 5)
            counter_id: ID du guichet appelant
            org_id: ID de l'organisation (isolation multi-tenant)
            actor: Utilisateur qui appelle (contrôle d'affectation et audit)

        Returns:
            Dict avec ticket_id, ticket_number, counter_name, user_id.
            {"success": False, ...} si la file est vide.

        Raises:
            HTTPException 400: Guichet fermé
            HTTPException 403: Agent non affecté à ce guichet
            HTTPException 404: Guichet ou service introuvable dans cette branch/org
        """
        counter = await self._get_callable_counter(counter_id, agency_id, org_id)
        await self._verify_agent_assignment(actor, counter)
        await self._get_service(service_id, agency_id, org_id)

        # Valeurs simples capturées AVANT toute boucle : un rollback expire les objets ORM
        counter_name = counter.name
        actor_id, actor_role = actor.id, actor.role.value

        ticket: Ticket | None = None
        for attempt in range(2):  # 2e tentative uniquement après une reconstruction depuis la base
            ticket = await self._pop_and_call(
                counter_id, counter_name, agency_id, service_id, org_id, actor_id, actor_role
            )
            if ticket is not None:
                break
            if attempt == 0 and await self.rebuild_queue(org_id, agency_id, service_id) > 0:
                logger.warning(
                    f"File Redis vide mais tickets WAITING en base : file reconstruite "
                    f"(branch={agency_id} service={service_id})"
                )
                continue
            return {"success": False, "message": "Aucun ticket en attente"}

        assert ticket is not None
        logger.info(f"Ticket appelé: {ticket.number} → guichet {counter_name} | org={org_id}")

        # Échéances : avertissement puis passage automatique en ABSENT
        await self._schedule_absent_deadlines(ticket)

        # Notifier le client (SMS + FCM) — fire and forget
        try:
            from app.services.notification_service import NotificationService
            notif_svc = NotificationService(self.db)
            await notif_svc.notify_ticket_called(ticket, counter_name)
        except Exception as e:
            logger.error(f"Notification TICKET_CALLED échouée (non bloquant): {e}")

        # Temps réel : « votre tour » (personnel) + « numéro appelé » (salle) + positions
        await publish_event(ticket.org_id, {
            "type": EventType.YOUR_TURN.value,
            "ticket_id": ticket.id,
            "ticket_number": ticket.number,
            "agency_id": ticket.agency_id,
            "counter_name": counter_name,
            "message": f"Présentez-vous au {counter_name}",
            "timeout_seconds": settings.ABSENT_TIMEOUT_SECONDS,
        })
        await publish_event(ticket.org_id, {
            "type": EventType.TICKET_CALLED.value,
            "called_number": ticket.number,
            "counter_name": counter_name,
            "agency_id": ticket.agency_id,
        })
        await self._publish_queue_snapshot(ticket.org_id, ticket.agency_id, ticket.service_id)

        return {
            "success": True,
            "ticket_id": ticket.id,
            "ticket_number": ticket.number,
            "counter_name": counter_name,
            "user_id": ticket.user_id,
        }

    async def start_serving(self, ticket_id: str, actor: User) -> dict:
        """
        Le guichetier confirme que le client est présent et le service commence.
        Transition : CALLED → SERVING

        Raises:
            HTTPException 400: Transition interdite
            HTTPException 403: Ticket d'un autre guichet / d'une autre branch
            HTTPException 404: Ticket introuvable
        """
        ticket = await self._get_ticket_for_staff(ticket_id, actor)
        await self._assert_staff_can_handle(actor, ticket)
        await self._transition(
            ticket, TicketStatus.SERVING,
            actor_id=actor.id, actor_role=actor.role.value,
        )
        ticket.served_at = datetime.now(timezone.utc)
        await self.db.commit()
        await clear_deadlines(ticket.org_id, ticket.id)  # présenté : plus d'absence possible
        return {"success": True, "ticket_id": ticket.id}

    async def complete_ticket(self, ticket_id: str, actor: User) -> dict:
        """
        Service terminé avec succès.
        Transition : SERVING → DONE

        Enregistre la durée réelle du service (alimente l'ETA) et libère le guichet.

        Raises:
            HTTPException 400: Transition interdite
            HTTPException 403: Ticket d'un autre guichet / d'une autre branch
            HTTPException 404: Ticket introuvable
        """
        ticket = await self._get_ticket_for_staff(ticket_id, actor)
        await self._assert_staff_can_handle(actor, ticket)
        await self._transition(
            ticket, TicketStatus.DONE,
            actor_id=actor.id, actor_role=actor.role.value,
        )
        ticket.done_at = datetime.now(timezone.utc)

        # Durée réelle de service (secondes) : source de l'ETA par moyenne glissante
        if ticket.served_at:
            ticket.service_seconds = max(
                0, int((ticket.done_at - as_utc(ticket.served_at)).total_seconds())
            )

        await self._release_counter(ticket)
        await self.db.commit()

        # Notifier le client (in-app) — fire and forget
        try:
            from app.services.notification_service import NotificationService
            notif_svc = NotificationService(self.db)
            await notif_svc.notify_ticket_done(ticket)
        except Exception as e:
            logger.error(f"Notification TICKET_DONE échouée (non bloquant): {e}")

        await publish_event(ticket.org_id, {
            "type": EventType.TICKET_DONE.value,
            "ticket_id": ticket.id,
            "ticket_number": ticket.number,
            "agency_id": ticket.agency_id,
        })
        return {"success": True, "ticket_id": ticket.id}

    async def mark_incomplete(self, ticket_id: str, actor: User, reason: str | None = None) -> dict:
        """
        Le service est interrompu sans être terminé : SERVING → INCOMPLETE.

        Le ticket reste « actif » (le client n'est pas libéré) jusqu'à ce qu'un agent le
        remette en file (requeue_ticket) ou le clôture (close_ticket), ou qu'il expire.

        Raises:
            HTTPException 400: Transition interdite (ticket non SERVING)
            HTTPException 403: Ticket d'un autre guichet / d'une autre branch
            HTTPException 404: Ticket introuvable
        """
        ticket = await self._get_ticket_for_staff(ticket_id, actor)
        await self._assert_staff_can_handle(actor, ticket)
        await self._transition(
            ticket, TicketStatus.INCOMPLETE,
            actor_id=actor.id, actor_role=actor.role.value,
            extra={"reason": reason} if reason else None,
        )
        await self._release_counter(ticket)
        await self.db.commit()
        return {"success": True, "ticket_id": ticket.id}

    async def mark_absent(self, ticket_id: str, actor: User) -> dict:
        """
        Le client ne s'est pas présenté après timeout (manuel : par l'agent).
        Transition : CALLED → ABSENT

        Raises:
            HTTPException 400: Transition interdite (ticket non CALLED)
            HTTPException 403: Ticket d'un autre guichet / d'une autre branch
            HTTPException 404: Ticket introuvable
        """
        ticket = await self._get_ticket_for_staff(ticket_id, actor)
        await self._assert_staff_can_handle(actor, ticket)
        await self._set_absent(ticket, actor_id=actor.id, actor_role=actor.role.value)
        logger.info(f"Ticket absent: {ticket.number}")
        return {"success": True, "ticket_id": ticket.id}

    async def transfer_ticket(
        self,
        ticket_id: str,
        target_service_id: str,
        actor: User,
        reason: str | None = None,
    ) -> dict:
        """
        Transfère un ticket appelé ou en service vers un autre service de la même branch.

        Flux : CALLED|SERVING → TRANSFERRED → WAITING. Le ticket garde son identité, son
        QR et son ancienneté (il ne repasse pas en fin de file) mais reçoit un nouveau
        numéro du service cible. TRANSFERRED est transitoire : il est tracé dans
        l'audit puis le ticket rejoint immédiatement la file du service cible, ce qui
        respecte la règle « 1 ticket actif ».

        Args:
            ticket_id: Ticket à transférer
            target_service_id: Service cible (même branch, actif, différent de l'actuel)
            actor: Agent / superviseur / admin
            reason: Motif libre (tracé dans l'audit)

        Returns:
            Dict avec le nouveau numéro, le service cible et la position dans sa file.

        Raises:
            HTTPException 400: Statut non transférable ou même service
            HTTPException 403: Ticket d'un autre guichet / d'une autre branch
            HTTPException 404: Ticket ou service cible introuvable
        """
        ticket = await self._get_ticket_for_staff(ticket_id, actor)
        await self._assert_staff_can_handle(actor, ticket)

        if ticket.service_id == target_service_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "SAME_SERVICE", "message": "Le ticket est déjà dans ce service"},
            )
        target = await self._get_service(target_service_id, ticket.agency_id, ticket.org_id)
        if not target.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "SERVICE_INACTIVE", "message": "Le service cible n'est pas disponible"},
            )

        source_service_id = ticket.service_id
        old_number = ticket.number

        # 1. Sortie de l'ancien service (journalisée avec l'ancien service_id)
        await self._transition(
            ticket, TicketStatus.TRANSFERRED,
            actor_id=actor.id, actor_role=actor.role.value,
            extra={
                "from_service_id": source_service_id,
                "to_service_id": target.id,
                "from_number": old_number,
                "reason": reason,
            },
        )
        await self._release_counter(ticket)

        # 2. Nouvelle identité dans le service cible
        sequence = await self._get_next_sequence(ticket.agency_id, target.ticket_prefix)
        ticket.prefix = target.ticket_prefix
        ticket.sequence = sequence
        ticket.number = f"{target.ticket_prefix}-{sequence:03d}"
        ticket.service_id = target.id
        ticket.counter_id = None
        ticket.counter_name = None
        ticket.called_at = None
        ticket.served_at = None

        # 3. Entrée dans la file cible (ancienneté conservée)
        await self._transition(
            ticket, TicketStatus.WAITING,
            actor_id=actor.id, actor_role=actor.role.value,
            extra={"transferred_from": source_service_id},
        )
        await AuditService(self.db).log(
            org_id=ticket.org_id, action=AuditAction.TICKET_TRANSFERRED,
            actor_id=actor.id, actor_role=actor.role.value,
            target_type="ticket", target_id=ticket.id,
            extra={"from_service_id": source_service_id, "to_service_id": target.id,
                   "from_number": old_number, "to_number": ticket.number, "reason": reason},
            commit=False,
        )
        await self.db.commit()

        await clear_deadlines(ticket.org_id, ticket.id)
        try:
            position = await add_to_queue(
                ticket.org_id, ticket.agency_id, ticket.service_id, ticket.id,
                PRIORITY_SCORES[ticket.priority],
                created_ms=to_epoch_ms(ticket.created_at),
            )
        except Exception as exc:  # la file sera reconstruite (auto-réparation) au prochain accès
            logger.error(f"Transfert {ticket.number}: ajout Redis échoué ({exc}) — auto-réparation attendue")
            position = 0

        await publish_event(ticket.org_id, {
            "type": EventType.TICKET_TRANSFERRED.value,
            "ticket_id": ticket.id,
            "ticket_number": ticket.number,
            "agency_id": ticket.agency_id,
            "service_id": target.id,
            "service_name": target.name,
            "position": position,
        })
        await self._publish_queue_snapshot(ticket.org_id, ticket.agency_id, source_service_id)
        await self._publish_queue_snapshot(ticket.org_id, ticket.agency_id, target.id)

        logger.info(f"Ticket transféré: {old_number} → {ticket.number} ({target.name}) par {actor.id}")
        return {
            "success": True,
            "ticket_id": ticket.id,
            "ticket_number": ticket.number,
            "service_id": target.id,
            "position": position,
        }

    async def decide_priority(
        self,
        ticket_id: str,
        actor: User,
        *,
        approve: bool = True,
        priority: str | None = None,
    ) -> dict:
        """
        Un agent confirme, attribue ou rejette la priorité d'un ticket en attente.

        La position est recalculée SANS faire perdre l'ancienneté : le score Redis
        reprend la date de création réelle du ticket.

        Args:
            ticket_id: Ticket WAITING concerné
            actor: Agent / superviseur / admin de la branch
            approve: False = rejette la demande du client (reste en standard)
            priority: Priorité à attribuer ; omise = priorité demandée par le client

        Returns:
            Dict avec la priorité effective et la nouvelle position.

        Raises:
            HTTPException 400: Ticket non WAITING, ou aucune priorité à appliquer/rejeter
            HTTPException 403: Hors de la branch de l'agent
            HTTPException 404: Ticket introuvable
        """
        ticket = await self._get_ticket_for_staff(ticket_id, actor)
        await self._assert_staff_in_branch(actor, ticket)

        if ticket.status != TicketStatus.WAITING:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "PRIORITY_NOT_CHANGEABLE",
                    "message": "La priorité ne peut être modifiée que pour un ticket en attente",
                },
            )

        previous = ticket.priority
        requested = ticket.requested_priority
        if not approve:
            if requested is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"code": "NO_PRIORITY_REQUEST", "message": "Aucune demande de priorité à rejeter"},
                )
            new_priority = previous
            audit_action = AuditAction.PRIORITY_REJECTED
        else:
            new_priority = TicketPriority(priority) if priority else requested
            if new_priority is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "NO_PRIORITY_REQUEST",
                        "message": "Aucune priorité demandée : précisez la priorité à attribuer",
                    },
                )
            audit_action = (
                AuditAction.PRIORITY_APPROVED
                if requested is not None and new_priority == requested
                else AuditAction.PRIORITY_SET
            )

        ticket.priority = new_priority
        ticket.requested_priority = None
        await AuditService(self.db).log(
            org_id=ticket.org_id, action=audit_action,
            actor_id=actor.id, actor_role=actor.role.value,
            target_type="ticket", target_id=ticket.id,
            extra={"previous": previous.value, "new": new_priority.value,
                   "requested": requested.value if requested else None},
            commit=False,
        )
        await self.db.commit()

        # Repositionnement : même ancienneté, nouvelle priorité
        position = await add_to_queue(
            ticket.org_id, ticket.agency_id, ticket.service_id, ticket.id,
            PRIORITY_SCORES[new_priority],
            created_ms=to_epoch_ms(ticket.created_at),
        )

        await publish_event(ticket.org_id, {
            "type": EventType.PRIORITY_UPDATED.value,
            "ticket_id": ticket.id,
            "ticket_number": ticket.number,
            "agency_id": ticket.agency_id,
            "priority": new_priority.value,
            "approved": approve,
            "position": position,
        })
        await self._publish_queue_snapshot(ticket.org_id, ticket.agency_id, ticket.service_id)
        return {
            "success": True,
            "ticket_id": ticket.id,
            "priority": new_priority.value,
            "position": position,
        }

    async def requeue_ticket(self, ticket_id: str, actor: User) -> dict:
        """
        Un agent remet en file un ticket INCOMPLETE : INCOMPLETE → WAITING.

        Le ticket garde sa priorité et son ancienneté (le service interrompu n'est pas
        la faute du client).

        Raises:
            HTTPException 400: Transition interdite (ticket non INCOMPLETE)
            HTTPException 403: Hors de la branch de l'agent
            HTTPException 404: Ticket introuvable
        """
        ticket = await self._get_ticket_for_staff(ticket_id, actor)
        await self._assert_staff_in_branch(actor, ticket)
        await self._transition(
            ticket, TicketStatus.WAITING,
            actor_id=actor.id, actor_role=actor.role.value,
            extra={"requeued_from": "incomplete"},
        )
        ticket.called_at = None
        ticket.served_at = None
        ticket.counter_id = None
        ticket.counter_name = None
        await AuditService(self.db).log(
            org_id=ticket.org_id, action=AuditAction.TICKET_REQUEUED,
            actor_id=actor.id, actor_role=actor.role.value,
            target_type="ticket", target_id=ticket.id, commit=False,
        )
        await self.db.commit()

        position = await add_to_queue(
            ticket.org_id, ticket.agency_id, ticket.service_id, ticket.id,
            PRIORITY_SCORES[ticket.priority],
            created_ms=to_epoch_ms(ticket.created_at),
        )
        await self._publish_queue_snapshot(ticket.org_id, ticket.agency_id, ticket.service_id)
        return {"success": True, "ticket_id": ticket.id, "position": position}

    async def close_ticket(self, ticket_id: str, actor: User, reason: str | None = None) -> dict:
        """
        Un agent clôture un ticket WAITING, ABSENT ou INCOMPLETE : → CANCELLED.

        Libère le client (règle « 1 ticket actif ») quand le ticket est abandonné.

        Raises:
            HTTPException 400: Transition interdite (ticket déjà terminé, appelé ou en service)
            HTTPException 403: Hors de la branch de l'agent
            HTTPException 404: Ticket introuvable
        """
        ticket = await self._get_ticket_for_staff(ticket_id, actor)
        await self._assert_staff_in_branch(actor, ticket)
        await self._transition(
            ticket, TicketStatus.CANCELLED,
            actor_id=actor.id, actor_role=actor.role.value,
            extra={"reason": reason} if reason else None,
        )
        await self.db.commit()
        await self._leave_queue(ticket)
        return {"success": True, "ticket_id": ticket.id}

    # ══ Tâches planifiées (appelées par QueueScheduler) ═══════════════════════

    async def auto_mark_absent(self, org_id: str, ticket_id: str) -> bool:
        """
        Passe automatiquement un ticket CALLED en ABSENT (échéance dépassée).

        Idempotent : si le client s'est présenté entre-temps (SERVING) ou si l'agent a
        déjà agi, ne fait rien.

        Returns:
            True si le ticket est passé en ABSENT, False sinon.
        """
        ticket = await self._find_ticket(ticket_id, org_id)
        if ticket is None or ticket.status != TicketStatus.CALLED:
            return False
        await self._set_absent(ticket, actor_id=None, actor_role="system")
        logger.info(f"Ticket {ticket.number} passé ABSENT automatiquement (délai dépassé)")
        return True

    async def send_absent_warning(self, org_id: str, ticket_id: str) -> bool:
        """
        Avertit le client appelé qu'il va être déclaré absent s'il ne se présente pas.

        Returns:
            True si l'avertissement a été émis (ticket toujours CALLED), False sinon.
        """
        ticket = await self._find_ticket(ticket_id, org_id)
        if ticket is None or ticket.status != TicketStatus.CALLED:
            return False
        seconds_left = max(0, settings.ABSENT_TIMEOUT_SECONDS - settings.ABSENT_WARNING_SECONDS)
        await publish_event(ticket.org_id, {
            "type": EventType.ABSENT_WARNING.value,
            "ticket_id": ticket.id,
            "ticket_number": ticket.number,
            "agency_id": ticket.agency_id,
            "counter_name": ticket.counter_name,
            "seconds_left": seconds_left,
            "message": f"Présentez-vous au {ticket.counter_name} dans {seconds_left} secondes",
        })
        try:
            from app.services.notification_service import NotificationService
            await NotificationService(self.db).notify_absent_warning(ticket, seconds_left)
        except Exception as e:
            logger.error(f"Notification ABSENT_WARNING échouée (non bloquant): {e}")
        return True

    async def expire_stale_tickets(self, now: datetime | None = None, batch: int = 200) -> dict[str, int]:
        """
        Clôt les tickets « actifs » restés bloqués, pour ne jamais bloquer un client à vie.

        Règles (délais configurables) :
          - WAITING  trop ancien (WAITING_TICKET_TTL_HOURS)       → CANCELLED
          - ABSENT   hors fenêtre de retour (ABSENT_RETURN_WINDOW) → CANCELLED
          - SERVING  oublié par l'agent (SERVING_TICKET_TTL_HOURS) → INCOMPLETE
          - INCOMPLETE non résolu (INCOMPLETE_TICKET_TTL_HOURS)    → CANCELLED

        Returns:
            Compteurs par règle appliquée.
        """
        now = now or datetime.now(timezone.utc)
        counts = {"waiting": 0, "absent": 0, "serving": 0, "incomplete": 0}

        rules: list[tuple[str, TicketStatus, TicketStatus, datetime, object]] = [
            ("waiting", TicketStatus.WAITING, TicketStatus.CANCELLED,
             now - timedelta(hours=settings.WAITING_TICKET_TTL_HOURS), Ticket.created_at),
            ("absent", TicketStatus.ABSENT, TicketStatus.CANCELLED,
             now - timedelta(minutes=settings.ABSENT_RETURN_WINDOW_MINUTES), Ticket.called_at),
            ("serving", TicketStatus.SERVING, TicketStatus.INCOMPLETE,
             now - timedelta(hours=settings.SERVING_TICKET_TTL_HOURS), Ticket.served_at),
            ("incomplete", TicketStatus.INCOMPLETE, TicketStatus.CANCELLED,
             now - timedelta(hours=settings.INCOMPLETE_TICKET_TTL_HOURS), Ticket.served_at),
        ]

        for name, from_status, to_status, cutoff, column in rules:
            result = await self.db.execute(
                select(Ticket.id).where(
                    Ticket.status == from_status,
                    column.isnot(None),
                    column < cutoff,
                ).limit(batch)
            )
            # Identifiants seulement : un rollback (sur une erreur) expire tous les objets
            # chargés ; chaque ticket est donc rechargé individuellement.
            for ticket_id in result.scalars().all():
                try:
                    ticket = (await self.db.execute(
                        select(Ticket).where(Ticket.id == ticket_id).execution_options(populate_existing=True)
                    )).scalar_one()
                    await self._transition(
                        ticket, to_status, actor_role="system",
                        extra={"reason": f"expired_{name}"},
                    )
                    if to_status == TicketStatus.INCOMPLETE:
                        await self._release_counter(ticket)
                    await self.db.commit()
                    await self._leave_queue(ticket)
                    counts[name] += 1
                except Exception as exc:  # un ticket en erreur ne bloque pas les autres
                    await self.db.rollback()
                    logger.error(f"Expiration du ticket {ticket_id} échouée ({name}): {exc}")
        if any(counts.values()):
            logger.info(f"Tickets expirés: {counts}")
        return counts

    async def alert_long_waits(self, limit: int = 500) -> int:
        """
        Règle métier (DÉCISION 9, « Rules Engine ») : prévient par SMS un client dont
        l'attente dépasse le seuil de sa branch (Branch.max_wait_minutes_alert).

        Limité aux branches ayant activé les rappels SMS (enable_sms_reminders) pour
        maîtriser le coût. Chaque ticket n'est alerté qu'une fois.

        Returns:
            Nombre d'alertes envoyées.
        """
        now = datetime.now(timezone.utc)
        result = await self.db.execute(
            select(Ticket, Branch.max_wait_minutes_alert)
            .join(Branch, Branch.id == Ticket.agency_id)
            .where(Ticket.status == TicketStatus.WAITING, Branch.enable_sms_reminders == True)  # noqa: E712
            .limit(limit)
        )
        sent = 0
        for ticket, threshold in result.all():
            waited_minutes = int((now - as_utc(ticket.created_at)).total_seconds() // 60)
            if waited_minutes < threshold:
                continue
            if not await mark_once(f"longwait:{ticket.id}", 24 * 3600):
                continue
            try:
                from app.services.notification_service import NotificationService
                await NotificationService(self.db).notify_long_wait_alert(ticket, waited_minutes, threshold)
                sent += 1
            except Exception as exc:
                logger.error(f"Alerte attente longue échouée pour {ticket.id}: {exc}")
        return sent

    # ══ File Redis : reconstruction et diffusion ═══════════════════════════════

    async def rebuild_queue(
        self,
        org_id: str | None = None,
        agency_id: str | None = None,
        service_id: str | None = None,
    ) -> int:
        """
        Reconstruit les files Redis depuis PostgreSQL (la source de vérité).

        Ajoute à la file chaque ticket WAITING qui n'y figure pas (ZADD NX : ne touche
        jamais un score existant), avec son score d'origine (priorité + ancienneté).
        Appelée au démarrage de l'API et en auto-réparation quand une file est vide.

        Args:
            org_id / agency_id / service_id: Filtres optionnels (aucun = toutes les files)

        Returns:
            Nombre de tickets réinsérés dans Redis.
        """
        query = select(
            Ticket.id, Ticket.org_id, Ticket.agency_id, Ticket.service_id,
            Ticket.priority, Ticket.created_at,
        ).where(Ticket.status == TicketStatus.WAITING)
        if org_id:
            query = query.where(Ticket.org_id == org_id)
        if agency_id:
            query = query.where(Ticket.agency_id == agency_id)
        if service_id:
            query = query.where(Ticket.service_id == service_id)

        restored = 0
        for tid, t_org, t_agency, t_service, t_priority, t_created in (await self.db.execute(query)).all():
            score = queue_score(PRIORITY_SCORES[t_priority], to_epoch_ms(t_created))
            if await add_to_queue_if_absent(t_org, t_agency, t_service, tid, score):
                restored += 1
        return restored

    async def rebuild_deadlines(self) -> int:
        """
        Reprogramme les échéances des tickets CALLED depuis PostgreSQL.

        Sans cela, un redémarrage de Redis efface les échéances : un client appelé
        mais jamais présenté ne serait plus jamais passé en ABSENT.

        Returns:
            Nombre de tickets dont l'échéance a été reprogrammée.
        """
        result = await self.db.execute(
            select(Ticket.id, Ticket.org_id, Ticket.called_at).where(
                Ticket.status == TicketStatus.CALLED, Ticket.called_at.isnot(None)
            )
        )
        count = 0
        for tid, org_id, called_at in result.all():
            base = as_utc(called_at).timestamp()
            await set_deadline(DEADLINE_WARNING, org_id, tid, base + settings.ABSENT_WARNING_SECONDS)
            await set_deadline(DEADLINE_ABSENT, org_id, tid, base + settings.ABSENT_TIMEOUT_SECONDS)
            count += 1
        return count

    async def _publish_queue_snapshot(self, org_id: str, agency_id: str, service_id: str) -> None:
        """
        Diffuse l'état d'une file à tous ses clients connectés (un seul message Redis).

        Chaque instance de l'API le transforme en `queue_update` individuel. Ne lève jamais.
        """
        try:
            ids = await get_queue_snapshot(org_id, agency_id, service_id)
            if not ids:
                return
            service = await self._get_service_row(service_id)
            avg_seconds = await self._avg_service_seconds(
                service_id, service.avg_duration_minutes if service else 5
            )
            await publish_event(org_id, queue_snapshot_event(
                agency_id=agency_id,
                service_id=service_id,
                positions={tid: index + 1 for index, tid in enumerate(ids)},
                current_called_number=await self._current_called_number(service_id),
                eta_seconds_per_position=avg_seconds,
            ))
        except Exception as exc:  # noqa: BLE001 — jamais bloquant
            logger.error(f"Snapshot de file non publié (service={service_id}): {exc}")

    # ══ ETA ════════════════════════════════════════════════════════════════════

    async def estimate_wait_minutes(self, service: Service | None, position: int) -> int:
        """
        Estime l'attente en minutes pour une position donnée (1 = prochain).

        ETA = personnes devant × durée moyenne d'un service, arrondie à la minute
        supérieure. La durée moyenne est celle des derniers services RÉELS du service
        (moyenne glissante), lissée par la valeur configurée tant que l'historique est court.

        Args:
            service: Le service (fournit la durée par défaut). None → 5 minutes.
            position: Position dans la file (1 = prochain)

        Returns:
            Minutes d'attente estimées (0 si c'est le prochain ou hors file).
        """
        if position <= 1:
            return 0
        default_minutes = service.avg_duration_minutes if service else 5
        service_id = service.id if service else None
        avg_seconds = await self._avg_service_seconds(service_id, default_minutes)
        return math.ceil((position - 1) * avg_seconds / 60)

    async def _avg_service_seconds(self, service_id: str | None, default_minutes: int) -> int:
        """
        Durée moyenne d'un service en secondes — moyenne glissante des ETA_SAMPLE_SIZE
        derniers tickets DONE de ce service.

        Avec n < ETA_SAMPLE_SIZE échantillons, la durée configurée complète la moyenne
        (lissage) : un seul service très long ne fausse pas tout l'ETA. Borné entre
        ETA_MIN_SERVICE_SECONDS et ETA_MAX_SERVICE_SECONDS.
        """
        default_seconds = max(1, default_minutes) * 60
        samples: list[int] = []
        if service_id:
            result = await self.db.execute(
                select(Ticket.service_seconds)
                .where(
                    Ticket.service_id == service_id,
                    Ticket.status == TicketStatus.DONE,
                    Ticket.service_seconds.isnot(None),
                )
                .order_by(Ticket.done_at.desc())
                .limit(ETA_SAMPLE_SIZE)
            )
            samples = [s for (s,) in result.all()]

        padding = max(0, ETA_SAMPLE_SIZE - len(samples))
        average = (sum(samples) + default_seconds * padding) / ETA_SAMPLE_SIZE
        return int(min(ETA_MAX_SERVICE_SECONDS, max(ETA_MIN_SERVICE_SECONDS, average)))

    # ══ Machine à états ════════════════════════════════════════════════════════

    async def _transition(
        self,
        ticket: Ticket,
        new_status: TicketStatus,
        actor_id: str | None = None,
        actor_role: str | None = None,
        counter_id: str | None = None,
        counter_name: str | None = None,
        extra: dict | None = None,
    ) -> None:
        """
        Applique une transition d'état sur un ticket.
        Vérifie que la transition est autorisée selon ALLOWED_TRANSITIONS.
        Crée une ligne QueueLog (audit trail + source Analytics).

        Args:
            ticket: Le ticket à faire évoluer
            new_status: Nouveau statut cible
            actor_id: ID de l'acteur (agent, client) — optionnel
            actor_role: Rôle de l'acteur — optionnel
            counter_id: ID du guichet impliqué — optionnel
            counter_name: Nom du guichet — optionnel
            extra: Détails JSON ajoutés à la ligne d'audit (motif, services source/cible…)

        Raises:
            HTTPException 400: Si la transition est interdite
        """
        allowed = ALLOWED_TRANSITIONS.get(ticket.status, [])
        if new_status not in allowed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "INVALID_TRANSITION",
                    "message": (
                        f"Transition interdite : {ticket.status.value} → {new_status.value}. "
                        f"Transitions autorisées depuis {ticket.status.value} : "
                        f"{[s.value for s in allowed] or 'aucune (état terminal)'}"
                    ),
                },
            )

        from_status = ticket.status
        ticket.status = new_status

        await self._log_action(
            ticket=ticket,
            action=STATUS_TO_LOG_ACTION.get(new_status, QueueLogAction.TICKET_CREATED),
            from_status=from_status,
            to_status=new_status,
            actor_id=actor_id,
            actor_role=actor_role,
            counter_id=counter_id,
            counter_name=counter_name,
            extra=extra,
        )

    async def _log_action(
        self,
        ticket: Ticket,
        action: QueueLogAction,
        from_status: TicketStatus | None = None,
        to_status: TicketStatus | None = None,
        actor_id: str | None = None,
        actor_role: str | None = None,
        counter_id: str | None = None,
        counter_name: str | None = None,
        elapsed_seconds: int | None = None,
        extra: dict | None = None,
    ) -> None:
        """
        Crée une ligne QueueLog immuable pour l'audit trail.

        Calcule automatiquement elapsed_seconds depuis la création du ticket
        si non fourni. Utilisé comme source de données pour Analytics (DÉCISION 9).

        Args:
            ticket: Le ticket concerné
            action: Type d'action enregistrée
            from_status: Statut précédent (None pour TICKET_CREATED)
            to_status: Nouveau statut
            actor_id: ID de l'acteur (agent ou client)
            actor_role: Rôle de l'acteur
            counter_id: Guichet impliqué
            counter_name: Nom du guichet
            elapsed_seconds: Durée depuis création (calculé auto si None)
            extra: Détails sérialisés en JSON dans extra_data
        """
        if elapsed_seconds is None and ticket.created_at:
            elapsed_seconds = int((datetime.now(timezone.utc) - as_utc(ticket.created_at)).total_seconds())

        log = QueueLog(
            org_id=ticket.org_id,
            ticket_id=ticket.id,
            agency_id=ticket.agency_id,
            service_id=ticket.service_id,
            action=action,
            from_status=from_status.value if from_status else None,
            to_status=to_status.value if to_status else (ticket.status.value),
            actor_id=actor_id,
            actor_role=actor_role,
            counter_id=counter_id or ticket.counter_id,
            counter_name=counter_name or ticket.counter_name,
            elapsed_seconds=elapsed_seconds,
            extra_data=json.dumps(extra, ensure_ascii=False) if extra else None,
        )
        self.db.add(log)

    # ══ Helpers privés ═════════════════════════════════════════════════════════

    async def _pop_and_call(
        self,
        counter_id: str,
        counter_name: str,
        agency_id: str,
        service_id: str,
        org_id: str,
        actor_id: str,
        actor_role: str,
    ) -> Ticket | None:
        """
        Retire le prochain ticket valide de la file et le passe à CALLED.

        Retourne None si la file est vide. Les entrées périmées (ticket annulé/supprimé
        resté dans Redis, ou déjà appelé ailleurs) sont sautées. Si l'écriture échoue
        après le pop, le ticket est remis à sa place d'origine avant de relancer l'erreur.

        Les paramètres sont des valeurs simples (str), pas des objets ORM : un
        rollback expire tous les objets de la session, et relire leurs attributs en
        async lèverait MissingGreenlet.
        """
        for _ in range(MAX_STALE_QUEUE_ENTRIES):
            popped = await pop_next_ticket(org_id, agency_id, service_id)
            if popped is None:
                return None
            candidate_id, score = popped

            try:
                now = datetime.now(timezone.utc)
                # Compare-and-set : une seule transaction peut faire passer ce ticket
                # de WAITING à CALLED, quelle que soit l'état de Redis.
                claimed = await self.db.execute(
                    update(Ticket)
                    .where(
                        Ticket.id == candidate_id,
                        Ticket.org_id == org_id,
                        Ticket.agency_id == agency_id,
                        Ticket.service_id == service_id,
                        Ticket.status == TicketStatus.WAITING,
                    )
                    .values(
                        status=TicketStatus.CALLED,
                        called_at=now,
                        counter_id=counter_id,
                        counter_name=counter_name,
                    )
                    .execution_options(synchronize_session=False)
                )
                if claimed.rowcount != 1:
                    # Rien n'a été modifié : pas de rollback (il expirerait inutilement
                    # tous les objets de la session). On passe à l'entrée suivante.
                    logger.warning(f"Entrée périmée ignorée dans la file: ticket={candidate_id}")
                    continue

                ticket = (await self.db.execute(
                    select(Ticket).where(Ticket.id == candidate_id)
                    .execution_options(populate_existing=True)
                )).scalar_one()

                # Attente réelle : création → appel
                ticket.actual_wait_minutes = max(
                    0, int((now - as_utc(ticket.created_at)).total_seconds() // 60)
                )
                await self._log_action(
                    ticket=ticket,
                    action=QueueLogAction.TICKET_CALLED,
                    from_status=TicketStatus.WAITING,
                    to_status=TicketStatus.CALLED,
                    actor_id=actor_id,
                    actor_role=actor_role,
                    counter_id=counter_id,
                    counter_name=counter_name,
                )
                await self.db.execute(
                    update(Counter)
                    .where(Counter.id == counter_id)
                    .values(current_ticket_id=ticket.id)
                )
                await self.db.commit()
                return ticket
            except Exception:
                # Le ticket a été retiré de Redis mais n'est pas passé à CALLED :
                # on le remet exactement à sa place pour qu'il ne soit pas perdu.
                await self.db.rollback()
                await restore_to_queue(org_id, agency_id, service_id, candidate_id, score)
                raise
        return None

    async def _schedule_absent_deadlines(self, ticket: Ticket) -> None:
        """Programme l'avertissement et le passage automatique en ABSENT. Ne lève jamais."""
        try:
            base = datetime.now(timezone.utc).timestamp()
            await set_deadline(DEADLINE_WARNING, ticket.org_id, ticket.id, base + settings.ABSENT_WARNING_SECONDS)
            await set_deadline(DEADLINE_ABSENT, ticket.org_id, ticket.id, base + settings.ABSENT_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Échéances non programmées pour {ticket.number}: {exc}")

    async def _set_absent(self, ticket: Ticket, *, actor_id: str | None, actor_role: str) -> None:
        """CALLED → ABSENT : libère le guichet, annule les échéances, notifie le client."""
        await self._transition(ticket, TicketStatus.ABSENT, actor_id=actor_id, actor_role=actor_role)
        await self._release_counter(ticket)
        await self.db.commit()
        await clear_deadlines(ticket.org_id, ticket.id)

        try:
            from app.services.notification_service import NotificationService
            await NotificationService(self.db).notify_ticket_absent(ticket)
        except Exception as e:
            logger.error(f"Notification TICKET_ABSENT échouée (non bloquant): {e}")

        await publish_event(ticket.org_id, {
            "type": EventType.TICKET_ABSENT.value,
            "ticket_id": ticket.id,
            "ticket_number": ticket.number,
            "agency_id": ticket.agency_id,
            "message": "Vous avez été déclaré absent. Vous pouvez revenir en file.",
            "return_window_minutes": settings.ABSENT_RETURN_WINDOW_MINUTES,
        })

    async def _leave_queue(self, ticket: Ticket) -> None:
        """Retire un ticket de la file Redis, annule ses échéances et rediffuse les positions."""
        try:
            await remove_from_queue(ticket.org_id, ticket.agency_id, ticket.service_id, ticket.id)
            await clear_deadlines(ticket.org_id, ticket.id)
        except Exception as exc:  # noqa: BLE001 — l'entrée périmée sera sautée par call_next
            logger.error(f"Retrait de la file échoué pour {ticket.number}: {exc}")
        await self._publish_queue_snapshot(ticket.org_id, ticket.agency_id, ticket.service_id)

    async def _build_response(self, ticket: Ticket) -> TicketResponse:
        """
        Construit la réponse complète d'un ticket (position, total, ETA).

        Auto-réparation : un ticket WAITING absent de Redis est réinséré à sa place d'origine.
        """
        position = 0
        total = 0
        eta = ticket.estimated_wait_minutes
        service = await self._get_service_row(ticket.service_id)

        if ticket.status == TicketStatus.WAITING:
            position = await get_queue_position(
                ticket.org_id, ticket.agency_id, ticket.service_id, ticket.id
            )
            if position == 0:
                position = await add_to_queue(
                    ticket.org_id, ticket.agency_id, ticket.service_id, ticket.id,
                    PRIORITY_SCORES[ticket.priority],
                    created_ms=to_epoch_ms(ticket.created_at),
                )
                logger.warning(f"Ticket {ticket.number} absent de Redis : remis en file (auto-réparation)")
            total = await get_queue_size(ticket.org_id, ticket.agency_id, ticket.service_id)
            eta = await self.estimate_wait_minutes(service, position)

        return self._to_response(ticket, position=max(0, position), total=total, eta=eta)

    @staticmethod
    def _to_response(ticket: Ticket, *, position: int, total: int, eta: int | None) -> TicketResponse:
        """Sérialise un Ticket ORM en TicketResponse (jamais d'objet ORM brut en sortie)."""
        return TicketResponse(
            id=ticket.id,
            number=ticket.number,
            status=ticket.status.value,
            priority=ticket.priority.value,
            requested_priority=ticket.requested_priority.value if ticket.requested_priority else None,
            agency_id=ticket.agency_id,
            service_id=ticket.service_id,
            qr_token=ticket.qr_token,
            position=position,
            total_in_queue=total,
            estimated_wait_minutes=eta,
            created_at=ticket.created_at,
            counter_name=ticket.counter_name,
        )

    async def _current_called_number(self, service_id: str) -> str | None:
        """Dernier numéro appelé dans un service (affiché dans la salle d'attente)."""
        result = await self.db.execute(
            select(Ticket.number)
            .where(Ticket.service_id == service_id, Ticket.called_at.isnot(None))
            .order_by(Ticket.called_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _get_service_row(self, service_id: str) -> Service | None:
        """Récupère un service par ID (sans contrôle de tenant : usage interne uniquement)."""
        result = await self.db.execute(select(Service).where(Service.id == service_id))
        return result.scalar_one_or_none()

    async def _enforce_daily_limit(self, branch: Branch) -> None:
        """
        Applique Branch.max_daily_tickets : nombre maximum de tickets créés par jour local.

        0 signifie « illimité » (c'est la convention de la configuration de la branch).

        Raises:
            HTTPException 409: Quota journalier atteint
        """
        if not branch.max_daily_tickets:
            return
        now_local = datetime.now(BURUNDI_TZ)
        day_start_utc = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        created_today = (await self.db.execute(
            select(func.count(Ticket.id)).where(
                Ticket.agency_id == branch.id,
                Ticket.created_at >= day_start_utc,
            )
        )).scalar() or 0
        if created_today >= branch.max_daily_tickets:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "DAILY_LIMIT_REACHED",
                    "message": "Le nombre maximum de tickets de la journée est atteint pour cette agence",
                },
            )

    async def _get_agency_for_creation(
        self, agency_id: str, user_org_id: str | None
    ) -> Branch:
        """
        Récupère une branch pour la création de ticket.

        Si user_org_id est fourni, vérifie l'appartenance à cette organisation.
        Si user_org_id est None (client OTP sans affiliation), cherche la branch globalement.
        """
        query = select(Branch).where(Branch.id == agency_id)
        if user_org_id:
            query = query.where(Branch.org_id == user_org_id)

        result = await self.db.execute(query)
        branch = result.scalar_one_or_none()
        if not branch:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "BRANCH_NOT_FOUND", "message": "Branch introuvable"},
            )
        return branch

    async def _get_agency(self, agency_id: str, org_id: str) -> Branch:
        """Récupère une branch en vérifiant son appartenance à l'organisation."""
        result = await self.db.execute(
            select(Branch).where(
                Branch.id == agency_id,
                Branch.org_id == org_id,
            )
        )
        branch = result.scalar_one_or_none()
        if not branch:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "BRANCH_NOT_FOUND", "message": "Branch introuvable"},
            )
        return branch

    async def _get_service(
        self, service_id: str, agency_id: str, org_id: str | None
    ) -> Service:
        """Récupère un service en vérifiant son appartenance à la branch et l'org."""
        query = select(Service).where(
            Service.id == service_id,
            Service.branch_id == agency_id,
        )
        if org_id:
            query = query.where(Service.org_id == org_id)

        result = await self.db.execute(query)
        service = result.scalar_one_or_none()
        if not service:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "SERVICE_NOT_FOUND", "message": "Service introuvable"},
            )
        return service

    async def _get_ticket_by_id(self, ticket_id: str, org_id: str) -> Ticket:
        """Récupère un ticket en vérifiant son org_id (isolation multi-tenant)."""
        result = await self.db.execute(
            select(Ticket).where(
                Ticket.id == ticket_id,
                Ticket.org_id == org_id,
            )
        )
        ticket = result.scalar_one_or_none()
        if not ticket:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "TICKET_NOT_FOUND", "message": "Ticket introuvable"},
            )
        return ticket

    async def _get_ticket_for_owner(self, ticket_id: str, user_id: str) -> Ticket:
        """
        Récupère un ticket par son ID ET son propriétaire (client sans organisation).

        Les clients OTP ont User.org_id = NULL : on ne peut pas filtrer par org_id
        de l'utilisateur. L'isolation vient du filtre user_id — un client ne peut
        jamais atteindre le ticket d'un autre. Retourne 404 (jamais 403) pour ne
        pas révéler l'existence d'un ticket appartenant à quelqu'un d'autre.

        Raises:
            HTTPException 404: Ticket introuvable ou appartenant à un autre user
        """
        result = await self.db.execute(
            select(Ticket).where(
                Ticket.id == ticket_id,
                Ticket.user_id == user_id,
            )
        )
        ticket = result.scalar_one_or_none()
        if not ticket:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "TICKET_NOT_FOUND", "message": "Ticket introuvable"},
            )
        return ticket

    async def _get_ticket_for_staff(self, ticket_id: str, actor: User) -> Ticket:
        """
        Récupère un ticket pour un membre du staff, dans le périmètre de son organisation.

        SUPER_ADMIN (équipe Tonde) peut atteindre le ticket de n'importe quelle organisation.
        Les autres sont limités à la leur : un ticket d'une autre org est « introuvable ».

        Raises:
            HTTPException 404: Ticket introuvable dans le périmètre de l'acteur
        """
        if actor.role == UserRole.SUPER_ADMIN:
            result = await self.db.execute(select(Ticket).where(Ticket.id == ticket_id))
            ticket = result.scalar_one_or_none()
            if not ticket:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"code": "TICKET_NOT_FOUND", "message": "Ticket introuvable"},
                )
            return ticket
        return await self._get_ticket_by_id(ticket_id, actor.org_id or "")

    async def _find_ticket(self, ticket_id: str, org_id: str) -> Ticket | None:
        """Récupère un ticket par ID dans l'org, ou None s'il n'existe pas (sans lever d'erreur)."""
        result = await self.db.execute(
            select(Ticket).where(Ticket.id == ticket_id, Ticket.org_id == org_id)
        )
        return result.scalar_one_or_none()

    async def _find_by_client_action(self, user_id: str, client_action_id: str) -> Ticket | None:
        """Retrouve le ticket déjà créé pour (user, client_action_id), s'il existe."""
        result = await self.db.execute(
            select(Ticket).where(
                Ticket.user_id == user_id,
                Ticket.client_action_id == client_action_id,
            )
        )
        return result.scalar_one_or_none()

    async def _assert_staff_can_handle(self, actor: User, ticket: Ticket) -> None:
        """
        Vérifie qu'un membre du staff peut agir sur un ticket appelé / en service.

        - SUPER_ADMIN, ADMIN_ORG : tout ticket de leur périmètre.
        - SUPERVISOR, ADMIN_AGENCY : tickets de leurs branches.
        - AGENT : uniquement les tickets de SON guichet (ticket.counter_id affecté).

        Raises:
            HTTPException 403: Ticket d'un autre guichet / d'une autre branch
        """
        if actor.role in ORG_WIDE_ROLES:
            return
        if actor.role == UserRole.AGENT:
            result = await self.db.execute(
                select(Employee.counter_id).where(
                    Employee.user_id == actor.id,
                    Employee.org_id == ticket.org_id,
                    Employee.status == EmployeeStatus.ACTIVE,
                    Employee.counter_id.isnot(None),
                )
            )
            assigned = {c for (c,) in result.all()}
            if ticket.counter_id is None or ticket.counter_id not in assigned:
                raise forbidden("Ce ticket n'est pas à votre guichet", code="TICKET_NOT_YOURS")
            return
        await self._assert_staff_in_branch(actor, ticket)

    async def _assert_staff_in_branch(self, actor: User, ticket: Ticket) -> None:
        """
        Vérifie que le ticket appartient à une branch où le staff est affecté.

        ADMIN_ORG / SUPER_ADMIN : toutes les branches de leur périmètre.

        Raises:
            HTTPException 403: Ticket d'une autre branch
        """
        if actor.role in ORG_WIDE_ROLES:
            return
        if ticket.agency_id not in await get_staff_branch_ids(self.db, actor):
            raise forbidden("Ce ticket appartient à une autre agence", code="BRANCH_FORBIDDEN")

    async def _get_active_ticket(self, user_id: str) -> Ticket | None:
        """
        Vérifie si l'utilisateur possède un ticket actif sur toute la plateforme.

        Règle DÉCISION 1 : vérification globale, sans filtre sur agency_id.
        Les statuts bloquants sont définis par ACTIVE_STATUSES.

        Args:
            user_id: ID de l'utilisateur à vérifier.

        Returns:
            Le ticket actif trouvé, ou None si l'utilisateur est libre.
        """
        result = await self.db.execute(
            select(Ticket).where(
                Ticket.user_id == user_id,
                Ticket.status.in_(ACTIVE_STATUSES),
            )
        )
        return result.scalars().first()

    async def _get_next_sequence(self, agency_id: str, prefix: str) -> int:
        """
        Génère le prochain numéro de séquence pour le jour en cours, de façon atomique.

        La séquence repart à 1 chaque jour à minuit HEURE DU BURUNDI (UTC+2).
        Le compteur est un INCR Redis (unique même sous création simultanée) ;
        s'il n'existe pas encore, il est initialisé depuis la plus grande
        séquence du jour déjà en base (reprise après redémarrage de Redis).

        Args:
            agency_id: Branch concernée
            prefix: Préfixe du service

        Returns:
            Numéro de séquence unique du jour (commence à 1)
        """
        now_local = datetime.now(BURUNDI_TZ)
        day = now_local.strftime("%Y%m%d")
        day_start_utc = now_local.replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(timezone.utc)

        async def load_seed() -> int:
            result = await self.db.execute(
                select(func.max(Ticket.sequence)).where(
                    Ticket.agency_id == agency_id,
                    Ticket.prefix == prefix,
                    Ticket.created_at >= day_start_utc,
                )
            )
            return result.scalar() or 0

        return await next_ticket_sequence(agency_id, prefix, day, load_seed)

    async def _get_callable_counter(
        self, counter_id: str, agency_id: str, org_id: str
    ) -> Counter:
        """
        Récupère un guichet depuis lequel on peut appeler un ticket.

        Le guichet doit appartenir à l'organisation ET à la branch demandée,
        être actif et ouvert.

        Raises:
            HTTPException 404: Guichet introuvable dans cette org/branch
            HTTPException 400: Guichet fermé
        """
        result = await self.db.execute(
            select(Counter).where(
                Counter.id == counter_id,
                Counter.org_id == org_id,
                Counter.agency_id == agency_id,
                Counter.is_active == True,  # noqa: E712
            )
        )
        counter = result.scalar_one_or_none()
        if not counter:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "COUNTER_NOT_FOUND", "message": "Guichet introuvable dans cette agence"},
            )
        if not counter.is_open:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "COUNTER_CLOSED", "message": "Ce guichet est fermé. Ouvrez-le avant d'appeler un ticket"},
            )
        return counter

    async def _verify_agent_assignment(self, actor: User, counter: Counter) -> None:
        """
        Vérifie que l'appelant a le droit d'utiliser ce guichet.

        Superviseurs et au-dessus : n'importe quel guichet de leur organisation.
        AGENT : uniquement le guichet qui lui est affecté (Employee actif).

        Raises:
            HTTPException 403: Agent non affecté à ce guichet
        """
        if actor.role in ANY_COUNTER_ROLES:
            return

        result = await self.db.execute(
            select(Employee.id).where(
                Employee.user_id == actor.id,
                Employee.org_id == counter.org_id,
                Employee.counter_id == counter.id,
                Employee.status == EmployeeStatus.ACTIVE,
            )
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "COUNTER_NOT_ASSIGNED",
                    "message": "Vous n'êtes pas affecté à ce guichet",
                },
            )

    async def _release_counter(self, ticket: Ticket) -> None:
        """
        Libère le guichet quand son ticket courant est terminé, absent ou transféré.

        N'efface current_ticket_id que s'il pointe encore sur CE ticket : un
        guichet qui a déjà rappelé quelqu'un d'autre n'est pas touché.
        """
        if not ticket.counter_id:
            return
        await self.db.execute(
            update(Counter)
            .where(
                Counter.id == ticket.counter_id,
                Counter.current_ticket_id == ticket.id,
            )
            .values(current_ticket_id=None)
        )
