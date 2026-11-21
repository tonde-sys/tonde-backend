"""
Service Tickets — Queue Engine de TONDE.

Responsabilités :
  - Création et suivi des tickets (idempotent : client_action_id)
  - Machine à états explicite (transitions contrôlées)
  - Appel atomique du prochain ticket (ZPOPMIN + compare-and-set en base)
  - Prévision ETA issue de l'AWTE et des observations réelles persistées
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
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import ORG_WIDE_ROLES, ROLE_RANK, forbidden, get_staff_branch_ids
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
from app.models.audit_log import AuditAction, AuditLog
from app.models.branch import Branch, Service
from app.models.counter import Counter
from app.models.employee import Employee, EmployeeStatus
from app.models.eta import EtaPrediction, EtaServiceProfile
from app.models.organization import Organization
from app.models.queue_log import QueueLog, QueueLogAction
from app.models.ticket import (
    ALLOWED_TRANSITIONS,
    PRIORITY_SCORES,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.models.user import User, UserRole
from app.schemas.ticket import (
    CreateTicketRequest,
    QueueEntry,
    QueueListResponse,
    TicketHistoryResponse,
    TicketResponse,
)
from app.services.audit_service import AuditService
from app.services.eta_service import EtaEstimator, ServiceQueueEstimate
from app.services.outbox import OutboxService
from app.websocket.events import EventType
from app.websocket.publisher import counter_activity_event, publish_event, queue_snapshot_event

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


def is_within_window(now_hhmm: str, opens: str | None, closes: str | None) -> bool:
    """
    Indique si une heure HH:MM tombe dans la fenêtre [opens, closes].

    Les chaînes sont zéro-complétées (HH:MM), donc la comparaison lexicographique
    est équivalente à la comparaison chronologique.

    Cas particuliers :
      - `opens == closes` → journée entière (convention « 00:00 → 00:00 ») ;
      - `opens > closes`  → créneau de nuit (ex. 22:00 → 02:00), qui enjambe minuit ;
      - valeur absente    → pas de contrainte (ouvert).

    Args:
        now_hhmm: Heure à tester, format HH:MM
        opens: Heure d'ouverture, format HH:MM
        closes: Heure de fermeture, format HH:MM
    """
    if not opens or not closes:
        return True
    if opens == closes:
        return True
    if opens < closes:
        return opens <= now_hhmm <= closes
    # Créneau de nuit : ouvert avant minuit OU après minuit
    return now_hhmm >= opens or now_hhmm <= closes


def is_branch_open_now(branch: Branch, now_local: datetime | None = None) -> bool:
    """
    Indique si une branch accepte des tickets à cet instant.

    Conditions, dans l'ordre :
      1. la branch est active ET ouverte (interrupteur manuel) ;
      2. si `operating_hours` est configuré, l'instant tombe dans le créneau du jour
         (un jour absent de `operating_hours` signifie « fermé ce jour-là ») ;
      3. sinon, l'instant tombe dans la fenêtre globale `opens_at` → `closes_at`.

    Le point 3 est le repli documenté par le modèle (`Branch.operating_hours` :
    « Si NULL, on utilise opens_at/closes_at comme horaire global »). Il était
    absent : une branch sans `operating_hours` acceptait des tickets 24 h/24, y
    compris la nuit et les jours de fermeture, en ignorant complètement les
    horaires saisis par l'administrateur.

    Args:
        branch: La branch à tester
        now_local: Instant à tester, en heure du Burundi (défaut : maintenant)
    """
    if not (branch.is_active and branch.is_open):
        return False

    now_local = now_local or datetime.now(BURUNDI_TZ)
    now_hhmm = now_local.strftime("%H:%M")

    hours = branch.operating_hours
    if hours:
        slot = hours.get(_WEEKDAYS[now_local.weekday()])
        if not slot:
            return False
        return is_within_window(now_hhmm, slot.get("open"), slot.get("close"))

    return is_within_window(now_hhmm, branch.opens_at, branch.closes_at)


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
        #
        # Depuis la migration 012, cet invariant est garanti par un index unique
        # partiel : le verrou n'est plus qu'une OPTIMISATION (moins de conflits
        # à gérer, message plus précis). Une panne Redis ne doit donc plus
        # interrompre la création — c'est précisément ce qui rendait la prise de
        # ticket dépendante de Redis alors que la base suffit.
        locked = True
        try:
            locked = await acquire_ticket_creation_lock(user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"Verrou de création indisponible (Redis) pour user={user_id}, "
                f"on s'appuie sur l'index unique : {exc}"
            )

        if not locked:
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
            # La libération ne doit jamais masquer le résultat (ou l'erreur) de la création.
            try:
                await release_ticket_creation_lock(user_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Verrou de création non libéré pour user={user_id}: {exc}")

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
            # Ordre de passage monotone, attribué par la base (migration 013).
            # C'est lui, et non l'horloge, qui départage deux tickets de même
            # priorité : deux créations dans la même milliseconde ne peuvent
            # plus être ordonnées par le hasard d'un UUID.
            queue_sequence=await self._next_queue_sequence(),
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
        except IntegrityError as exc:
            # Course inter-instances sur (user_id, client_action_id) : le gagnant a déjà créé
            await self.db.rollback()
            if data.client_action_id:
                replay = await self._find_by_client_action(user_id, data.client_action_id)
                if replay is not None:
                    return await self._build_response(replay)
            # Violation de `uq_tickets_active_per_user` (migration 012) : une autre
            # requête — ou une autre instance — a créé un ticket actif entre notre
            # `SELECT` et notre `INSERT`. C'est exactement ce que l'index doit
            # arrêter, et c'est la base qui l'a arrêté, pas nous. On répond le
            # même 409 que le contrôle applicatif, avec le ticket gagnant, pour
            # que le client voie son vrai ticket au lieu d'une erreur serveur.
            winner = await self._get_active_ticket(user_id)
            if winner is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "TICKET_ALREADY_ACTIVE",
                        "message": (
                            f"Vous avez déjà un ticket actif ({winner.number} — "
                            f"statut : {winner.status.value}). "
                            "Attendez qu'il soit terminé ou annulez-le avant d'en prendre un nouveau."
                        ),
                        "active_ticket_id": winner.id,
                        "active_ticket_number": winner.number,
                    },
                ) from exc
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
        #
        # Si Redis échoue, on ne détruit plus le ticket. L'ancien comportement
        # (annuler puis répondre 503) avait un coût absurde : la base avait
        # accepté la demande, et le client repartait sans ticket, obligé de
        # recommencer. Le ticket reste donc WAITING avec une position inconnue
        # (0 = hors file), et la réparation périodique du scheduler le remettra
        # dans la file au retour de Redis (`rebuild_queue`). C'est le même
        # arbitrage que `return_to_queue` (constat P1-11) : on dégrade, on ne
        # perd pas le travail déjà fait.
        queue_degraded = False
        try:
            position = await add_to_queue(
                ticket.org_id, ticket.agency_id, ticket.service_id, ticket.id,
                PRIORITY_SCORES[TicketPriority.STANDARD],
                sequence=ticket.queue_sequence,
            )
        except Exception as exc:  # noqa: BLE001
            queue_degraded = True
            position = 0
            logger.error(
                f"Ticket {ticket.number} créé en base mais Redis indisponible "
                f"(position inconnue jusqu'à réparation) : {exc}"
            )

        # La taille de la file est une information d'affichage : son échec ne doit
        # jamais annuler un ticket DÉJÀ dans la file (constat P2-15). Avant ce
        # correctif, une erreur sur `zcard` suffisait à annuler le ticket en base
        # tout en le laissant dans le ZSET — une entrée fantôme que `_pop_and_call`
        # devait nettoyer plus tard. On dégrade l'affichage, pas la prise de ticket.
        if queue_degraded:
            total = await self._count_waiting_in_db(ticket.service_id)
        else:
            try:
                total = await get_queue_size(ticket.org_id, ticket.agency_id, ticket.service_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"Taille de file indisponible pour {ticket.number} (affichage dégradé) : {exc}"
                )
                total = position
        eta = await self.estimate_ticket_wait_minutes(
            ticket, service, persist_forecast=True
        )
        ticket.estimated_wait_minutes = eta
        await self.db.commit()

        logger.info(
            f"Ticket créé: {ticket_number} | org={ticket.org_id} | position={position} | ETA={eta}min"
            + (f" | priorité demandée={requested_priority.value}" if requested_priority else "")
        )

        # La création n'est pas une transition : `_transition` n'est pas appelé ici,
        # donc l'écran de salle et les guichets ne sauraient rien avant le prochain
        # sondage. C'est le cas le plus fréquent de la journée (un client arrive),
        # et c'est précisément celui que le poste desktop devait attendre 7 secondes.
        await self._publish_counter_activity(ticket, "ticket_created")
        await self._publish_queue_snapshot(ticket.org_id, ticket.agency_id, ticket.service_id)

        response = self._to_response(ticket, position=position, total=total, eta=eta)
        response.queue_degraded = queue_degraded
        await self._fill_context_names(response, ticket.service_id, ticket.org_id)
        await self._fill_eta_metadata(response, ticket)
        return response

    # ══ Consultation ═══════════════════════════════════════════════════════════

    async def get_ticket(self, ticket_id: str, viewer: User) -> TicketResponse:
        """
        Retourne les informations actuelles d'un ticket, selon qui le demande.

        Deux chemins distincts, choisis sur le RÔLE et non sur la présence d'un
        org_id — c'était le défaut : un SUPER_ADMIN a `org_id = None` (il n'est
        rattaché à aucune organisation), tombait donc dans le chemin CLIENT et
        recevait 404 sur un ticket parfaitement valide.

        - CLIENT : uniquement ses propres tickets.
            * sans organisation (OTP) → recherche par propriétaire ;
            * avec organisation (code d'invitation) → recherche dans l'org puis
              contrôle de propriété (403).
        - STAFF (agent et au-dessus) : ticket de son organisation, et de sa
          branch pour les rôles non transverses (`_assert_staff_in_branch`).
          Sans ce contrôle, un agent de l'agence A lisait les tickets de
          l'agence B de la même organisation.

        Auto-réparation : si le ticket est WAITING mais absent de Redis (Redis vidé ou
        redémarré), il est remis en file à sa place d'origine avant de calculer sa position.

        Args:
            ticket_id: ID du ticket
            viewer: Utilisateur authentifié (client ou membre du staff)

        Raises:
            HTTPException 403: Ticket d'un autre client, ou d'une autre branch
            HTTPException 404: Ticket introuvable dans le périmètre du demandeur
        """
        if viewer.role == UserRole.CLIENT:
            if viewer.org_id is None:
                # _get_ticket_for_owner filtre déjà sur user_id : 404, jamais 403.
                ticket = await self._get_ticket_for_owner(ticket_id, viewer.id)
            else:
                ticket = await self._get_ticket_by_id(ticket_id, viewer.org_id)
                if ticket.user_id != viewer.id:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail={
                            "code": "FORBIDDEN",
                            "message": "Accès non autorisé à ce ticket",
                        },
                    )
        else:
            # Staff : périmètre organisation (SUPER_ADMIN = toutes), puis branch.
            ticket = await self._get_ticket_for_staff(ticket_id, viewer)
            await self._assert_staff_in_branch(viewer, ticket)

        return await self._build_response(ticket)

    async def get_active_ticket(self, user_id: str) -> TicketResponse | None:
        """
        Ticket encore actif du client, ou None s'il est libre.

        Miroir public de `_get_active_ticket`, exposé au mobile. Sans cette
        route, le poste devait deviner son état de deux manières également
        fausses : garder l'identifiant du ticket localement (perdu à la
        réinstallation, et périmé dès que le guichet clôture) ou filtrer
        `/history` (dont la pagination peut masquer le ticket actif s'il n'est
        pas sur la première page).

        Le statut retenu est celui de `ACTIVE_STATUSES` — la MÊME liste que
        l'invariant « un seul ticket actif ». Deux définitions divergentes
        produiraient un mobile qui propose de prendre un ticket que le serveur
        refusera en 409.

        Args:
            user_id: Propriétaire dont on cherche le ticket.

        Returns:
            Le ticket avec position et attente estimée, ou None.
        """
        ticket = await self._get_active_ticket(user_id)
        if ticket is None:
            return None
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
            "estimated_wait_minutes": response.estimated_wait_minutes,
            "estimated_wait_confidence": response.estimated_wait_confidence,
            "eta_calculated_at": response.eta_calculated_at.isoformat() if response.eta_calculated_at else None,
            "eta_version": response.eta_version,
            "current_called_number": await self._current_called_number(ticket.service_id, ticket.org_id) or "",
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

        items = []
        for t in tickets:
            position, queue_total = 0, 0
            if t.status == TicketStatus.WAITING:
                # Un ticket encore en file doit afficher sa vraie position.
                # Avant, `get_history` renvoyait 0 pour tout le monde : le mobile
                # annonçait « position 0 » à un client qui attendait (P1-12).
                try:
                    position = await get_queue_position(
                        t.org_id, t.agency_id, t.service_id, t.id
                    )
                    queue_total = await get_queue_size(t.org_id, t.agency_id, t.service_id)
                except Exception as exc:
                    logger.warning(
                        f"File indisponible pour l'historique du ticket {t.number} ({exc}) "
                        ": position laissée à 0"
                    )
            eta = t.estimated_wait_minutes
            if t.status == TicketStatus.WAITING:
                service = await self._get_service_row(t.service_id, t.org_id)
                eta = await self.estimate_ticket_wait_minutes(
                    t,
                    service,
                )
            items.append(self._to_response(
                t, position=position, total=queue_total, eta=eta
            ))

        return TicketHistoryResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            has_next=(offset + page_size) < total,
        )

    async def list_queue(
        self,
        agency_id: str,
        service_id: str,
        actor: User,
    ) -> QueueListResponse:
        """
        File d'attente d'un service, vue par le staff (constat P1-8 de l'audit).

        Avant cet endpoint, le staff n'avait aucun moyen de voir qui attendait :
        il fallait connaître l'UUID d'un ticket pour le clôturer, et la
        supervision d'une agence bondée se faisait à l'aveugle.

        L'ordre est calculé depuis PostgreSQL, PAS depuis Redis :
          - la base est la source de vérité, l'ordre reste correct même si Redis
            est indisponible (or c'est précisément en cas d'incident qu'on a
            besoin de voir la file) ;
          - il reproduit exactement l'ordre d'appel, puisque `queue_score` classe
            par priorité puis par `created_at`, comme le tri ci-dessous.

        Args:
            agency_id: Branch dont on liste la file
            service_id: Service (chaque service a sa propre file)
            actor: Utilisateur authentifié, rôle ≥ AGENT

        Returns:
            QueueListResponse : compteurs + entrées WAITING (ordonnées), puis
            CALLED et SERVING.

        Raises:
            HTTPException 403: Rôle insuffisant ou branche hors périmètre
            HTTPException 404: Branche ou service introuvable dans l'organisation
        """
        # Un client n'a rien à faire ici, même si le routeur le filtre déjà :
        # la garde est dans le service pour protéger les appels internes.
        if ROLE_RANK[actor.role] < ROLE_RANK[UserRole.AGENT]:
            raise forbidden("Réservé au personnel d'agence", code="STAFF_ONLY")

        branch = await self._get_agency(agency_id, actor.org_id)

        # Périmètre : un agent/superviseur ne voit que ses branches d'affectation.
        if actor.role not in ORG_WIDE_ROLES:
            if branch.id not in await get_staff_branch_ids(self.db, actor):
                raise forbidden(
                    "Vous n'êtes pas affecté à cette agence", code="BRANCH_FORBIDDEN"
                )

        service = await self._get_service(service_id, branch.id, branch.org_id)

        rows = (
            await self.db.execute(
                select(Ticket).where(
                    Ticket.org_id == branch.org_id,
                    Ticket.agency_id == branch.id,
                    Ticket.service_id == service.id,
                    Ticket.status.in_(
                        [TicketStatus.WAITING, TicketStatus.CALLED, TicketStatus.SERVING]
                    ),
                )
            )
        ).scalars().all()

        now = datetime.now(timezone.utc)
        waiting = [t for t in rows if t.status == TicketStatus.WAITING]
        called = [t for t in rows if t.status == TicketStatus.CALLED]
        serving = [t for t in rows if t.status == TicketStatus.SERVING]

        # Même clé de tri que `queue_score` : priorité décroissante, puis FIFO.
        waiting.sort(key=lambda t: (-PRIORITY_SCORES[t.priority], as_utc(t.created_at)))
        called.sort(key=lambda t: as_utc(t.called_at or t.created_at))
        serving.sort(key=lambda t: as_utc(t.served_at or t.called_at or t.created_at))

        entries: list[QueueEntry] = []
        for index, ticket in enumerate(waiting, start=1):
            entries.append(self._queue_entry(ticket, position=index, now=now))
        for ticket in called + serving:
            # Position 0 = hors file d'attente : le ticket est déjà au guichet.
            entries.append(self._queue_entry(ticket, position=0, now=now))

        waits = [e.waiting_since_minutes for e in entries[: len(waiting)]]
        return QueueListResponse(
            agency_id=branch.id,
            service_id=service.id,
            service_name=service.name,
            waiting_count=len(waiting),
            called_count=len(called),
            serving_count=len(serving),
            average_wait_minutes=round(sum(waits) / len(waits)) if waits else None,
            entries=entries,
            generated_at=now,
        )

    @staticmethod
    def _queue_entry(ticket: Ticket, *, position: int, now: datetime) -> QueueEntry:
        """Sérialise un ticket pour la liste de file (aucun objet ORM en sortie)."""
        # L'ancienneté se mesure depuis l'entrée dans l'état courant, pas depuis
        # la création : un ticket rappelé après une absence ne doit pas afficher
        # 40 minutes d'attente alors qu'il vient de revenir en file.
        if ticket.status == TicketStatus.SERVING:
            since = ticket.served_at or ticket.called_at or ticket.created_at
        elif ticket.status == TicketStatus.CALLED:
            since = ticket.called_at or ticket.created_at
        else:
            since = ticket.created_at
        minutes = max(0, int((now - as_utc(since)).total_seconds() // 60))
        return QueueEntry(
            ticket_id=ticket.id,
            number=ticket.number,
            status=ticket.status.value,
            priority=ticket.priority.value,
            requested_priority=(
                ticket.requested_priority.value if ticket.requested_priority else None
            ),
            position=position,
            waiting_since_minutes=minutes,
            created_at=ticket.created_at,
            counter_name=ticket.counter_name,
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

        Trois conditions sont vérifiées ici, et elles n'existaient pas
        (spécification §3.4) :

          - la fenêtre de récupération n'est pas écoulée ;
          - le plafond d'absences n'est pas atteint ;
          - l'utilisateur n'a pas déjà un AUTRE ticket actif (garde-fou contre
            une donnée incohérente héritée d'une époque où la règle n'était pas
            atomique).

        Le ticket conserve sa tranche de priorité et revient en FIN de cette
        tranche (spécification §4.3). Le code précédent forçait STANDARD à vie :
        une femme enceinte ou une personne âgée perdait définitivement sa priorité
        après un seul appel manqué. La pénalité de temps est déjà assurée par le
        retour en fin de tranche — la double peine n'ajoutait rien qu'une injustice.

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
            HTTPException 409: Fenêtre expirée, plafond atteint, ou autre ticket actif
        """
        if user_id is not None:
            ticket = await self._get_ticket_for_owner(ticket_id, user_id)
        elif actor is not None:
            ticket = await self._get_ticket_for_staff(ticket_id, actor)
            await self._assert_staff_in_branch(actor, ticket)
        else:
            ticket = await self._get_ticket_by_id(ticket_id, org_id)

        # La machine à états refuse déjà tout statut autre qu'ABSENT (400).
        # Ces contrôles ne s'appliquent donc qu'aux tickets réellement absents.
        if ticket.status == TicketStatus.ABSENT:
            now = datetime.now(timezone.utc)

            deadline = self._recovery_deadline(ticket)
            if deadline is not None and now >= deadline:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "RECOVERY_WINDOW_EXPIRED",
                        "message": (
                            "La fenêtre de retour est écoulée : ce ticket ne peut plus "
                            "être réactivé. Prenez un nouveau ticket."
                        ),
                        "recovery_expires_at": deadline.isoformat(),
                    },
                )

            missed = ticket.missed_count or 0
            if missed >= settings.MAX_MISSED_COUNT:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "MAX_MISSED_COUNT_REACHED",
                        "message": (
                            f"Vous avez été absent {missed} fois : la reprise n'est plus "
                            "possible. Prenez un nouveau ticket."
                        ),
                        "missed_count": missed,
                        "max_missed_count": settings.MAX_MISSED_COUNT,
                    },
                )

            # Garde-fou : un autre ticket actif rendrait la règle « un seul ticket
            # actif global » fausse. On exclut CE ticket, qui est lui-même actif.
            autre = (await self.db.execute(
                select(Ticket.id).where(
                    Ticket.user_id == ticket.user_id,
                    Ticket.id != ticket.id,
                    Ticket.status.in_(ACTIVE_STATUSES),
                ).limit(1)
            )).scalar_one_or_none()
            if autre is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "ACTIVE_TICKET_ALREADY_EXISTS",
                        "message": "Vous avez déjà un autre ticket en cours.",
                        "active_ticket_id": autre,
                    },
                )

        await self._transition(
            ticket, TicketStatus.WAITING,
            actor_id=user_id or (actor.id if actor else None),
            actor_role=UserRole.CLIENT.value if user_id else (actor.role.value if actor else None),
            extra={"missed_count": ticket.missed_count or 0},
        )
        # La priorité est CONSERVÉE (spécification §4.3). Le retour en fin de
        # tranche suffit, et il est désormais FORMEL : la reprise reçoit une
        # NOUVELLE séquence de file (spécification §4.2), donc postérieure à
        # tous les tickets déjà en attente — y compris ceux de sa propre tranche.
        ticket.requeued_at = datetime.now(timezone.utc)
        ticket.recovery_expires_at = None
        ticket.grace_expires_at = None
        ticket.queue_sequence = await self._next_queue_sequence()
        await self.db.commit()

        # Constat P1-11 : `add_to_queue` n'était pas protégé. Une panne Redis
        # laissait le ticket WAITING en base mais absent du ZSET ; le client
        # restait bloqué par la règle « 1 ticket actif » jusqu'à ce qu'un
        # `call_next` ou une lecture de son ticket déclenche l'auto-réparation.
        # Sur une agence sans activité, cela pouvait durer des heures.
        #
        # On ne peut pas annuler la transition (elle est déjà commitée, et
        # WAITING → ABSENT n'est pas une transition autorisée) : on répond donc
        # la vérité au client (`queue_degraded`) au lieu de simuler un succès
        # complet, et on laisse la réparation périodique du scheduler remettre
        # le ticket dans la file dès le retour de Redis (voir
        # QueueScheduler.run_once → rebuild_queue).
        queue_degraded = False
        try:
            await add_to_queue(
                ticket.org_id, ticket.agency_id, ticket.service_id, ticket.id,
                PRIORITY_SCORES[ticket.priority],
                sequence=ticket.queue_sequence,
            )
        except Exception as exc:  # noqa: BLE001
            queue_degraded = True
            logger.error(
                f"Ticket {ticket.number} remis en file en base mais Redis indisponible "
                f"(position indisponible jusqu'à réparation) : {exc}"
            )

        if not queue_degraded:
            await self._publish_queue_snapshot(ticket.org_id, ticket.agency_id, ticket.service_id)
        return {
            "success": True,
            "ticket_id": ticket.id,
            "queue_degraded": queue_degraded,
            "priority": ticket.priority.value,
            "missed_count": ticket.missed_count or 0,
        }

    def _recovery_deadline(self, ticket: Ticket) -> datetime | None:
        """
        Fin de la fenêtre de reprise d'un ticket ABSENT.

        `recovery_expires_at` est la donnée de référence, ancrée sur l'ABSENCE.
        Repli sur `last_missed_at` puis `called_at` pour les tickets antérieurs à
        la migration 011 : mieux vaut une échéance approchée qu'un ticket
        immortel. Retourne None si aucune date n'est exploitable — auquel cas la
        reprise est autorisée, pour ne pas punir un client à cause d'un champ
        manquant.
        """
        if ticket.recovery_expires_at is not None:
            return as_utc(ticket.recovery_expires_at)
        anchor = ticket.last_missed_at or ticket.called_at
        if anchor is None:
            return None
        return as_utc(anchor) + timedelta(minutes=settings.ABSENT_RETURN_WINDOW_MINUTES)

    # ══ Actions du guichetier ══════════════════════════════════════════════════

    async def requeue_after_counter_close(self, ticket: Ticket) -> bool:
        """
        Remet en file un ticket CALLED ou SERVING dont le guichet vient de fermer.

        Le problème corrigé (P1-7 de l'audit) : `CounterService.close` remettait
        `current_ticket_id` à NULL sans toucher au ticket. Le client restait
        CALLED, personne ne le servait, et le scheduler le passait en ABSENT
        180 s plus tard — avec 30 minutes de pénalité avant de pouvoir revenir.
        Le client était présent : c'est le guichet qui a fermé.

        Le ticket retrouve sa place d'origine (le score de file utilise
        `created_at`, donc son ancienneté réelle), pas la fin de file.

        Returns:
            True si le ticket a été remis en file, False s'il n'était pas appelé.
        """
        if ticket.status not in (TicketStatus.CALLED, TicketStatus.SERVING):
            return False

        previous = ticket.status
        await self._transition(
            ticket, TicketStatus.WAITING,
            actor_role="system",
            extra={"reason": "counter_closed", "from_status": previous.value},
        )
        ticket.counter_id = None
        ticket.counter_name = None
        ticket.called_at = None
        ticket.served_at = None
        await self.db.commit()

        await add_to_queue(
            ticket.org_id, ticket.agency_id, ticket.service_id, ticket.id,
            PRIORITY_SCORES[ticket.priority],
            sequence=ticket.queue_sequence,
        )
        await self._publish_queue_snapshot(ticket.org_id, ticket.agency_id, ticket.service_id)

        logger.info(
            f"Ticket {ticket.number} remis en file ({previous.value} → waiting) : guichet fermé"
        )
        return True

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
          - Un guichet ne peut pas appeler un nouveau ticket tant qu'il traite le
            précédent (CALLED ou SERVING) : sinon le client en cours restait
            CALLED sans personne pour le servir, et le scheduler le déclarait
            ABSENT 3 minutes plus tard alors qu'il était présent.
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
            HTTPException 409: Le guichet traite déjà un client (COUNTER_BUSY)
        """
        # Verrou de ligne sur le guichet : deux appels simultanés depuis le même
        # poste (double clic, deux onglets) ne peuvent pas franchir le contrôle
        # « guichet libre » tous les deux. Le verrou est relâché au premier commit.
        counter = await self._get_callable_counter(counter_id, agency_id, org_id, for_update=True)
        await self._verify_agent_assignment(actor, counter)
        await self._get_service(service_id, agency_id, org_id)
        await self._assert_counter_free(counter)

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

    async def recall(self, ticket_id: str, actor: User) -> dict:
        """
        Relance l'annonce d'un ticket DÉJÀ appelé.

        Pourquoi ce endpoint existe : quand un client n'entend pas son numéro (salle
        bruyante, il était dehors), l'agent n'avait aucun moyen de le rappeler. Sa
        seule option était de le déclarer ABSENT, ce qui renvoyait le client
        derrière tout le monde — une punition pour un problème d'audition. Le poste
        desktop a donc dû masquer son bouton « Rappeler » hors mode démonstration.

        Ce que fait une relance :
          - rediffuse `your_turn` (téléphone du client) et `ticket_called` (salle) ;
          - renvoie le SMS / push d'appel ;
          - **remet le compte à rebours à zéro** (avertissement + passage en ABSENT),
            sans quoi la relance serait un bruit sans effet ;
          - prévient les écrans de guichet ;
          - laisse une trace d'audit.

        Limite : `MAX_RECALLS_PER_TICKET` (défaut 2). Chaque relance envoie un SMS
        réel ; sans plafond, un agent qui s'acharne sur le bouton fait exploser la
        facture et harcèle un client qui a peut-être simplement quitté la salle.

        Args:
            ticket_id: Ticket concerné
            actor: Agent ou superviseur qui relance

        Returns:
            Dict avec ticket_number, counter_name, recall_count, recalls_left,
            timeout_seconds.

        Raises:
            HTTPException 404: Ticket introuvable dans le périmètre de l'acteur
            HTTPException 403: Ticket d'un autre guichet / d'une autre branch
            HTTPException 409: Ticket pas en état CALLED, ou plafond de relances atteint
        """
        ticket = await self._get_ticket_for_staff(ticket_id, actor)
        await self._assert_staff_can_handle(actor, ticket)

        if ticket.status != TicketStatus.CALLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "TICKET_NOT_CALLED",
                    "message": (
                        f"Seul un ticket appelé peut être relancé "
                        f"(statut actuel : {ticket.status.value})"
                    ),
                },
            )

        if not ticket.counter_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "TICKET_WITHOUT_COUNTER",
                    "message": "Ce ticket n'est associé à aucun guichet : rien à relancer",
                },
            )

        counter = (await self.db.execute(
            select(Counter).where(Counter.id == ticket.counter_id)
        )).scalar_one_or_none()
        if counter is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "COUNTER_NOT_FOUND", "message": "Guichet du ticket introuvable"},
            )
        await self._verify_agent_assignment(actor, counter)

        # Plafond : compté sur le journal d'audit, pas sur un compteur Redis. Le
        # journal est la source durable — un redémarrage de Redis ne doit pas
        # rouvrir le droit d'envoyer trois SMS de plus au même client.
        recall_count = (await self.db.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == AuditAction.TICKET_RECALLED,
                AuditLog.target_id == ticket.id,
            )
        )).scalar() or 0
        if recall_count >= settings.MAX_RECALLS_PER_TICKET:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "RECALL_LIMIT_REACHED",
                    "message": (
                        f"Ce ticket a déjà été relancé {recall_count} fois. "
                        "Déclarez le client absent s'il ne s'est pas présenté."
                    ),
                    "recall_count": recall_count,
                    "max_recalls": settings.MAX_RECALLS_PER_TICKET,
                },
            )

        counter_name = counter.name
        ticket_number = ticket.number

        # 1. Le compte à rebours repart de zéro : une relance sans nouveau délai
        #    serait une annonce dans le vide (le ticket passerait ABSENT pendant
        #    que le client arrive).
        await self._schedule_absent_deadlines(ticket)

        #    Le délai de GRÂCE repart aussi : l'agent vient de réannoncer, il ne
        #    peut pas déclarer absent dans la seconde. Sans cela, relancer puis
        #    déclarer absent immédiatement aurait été un contournement du contrôle.
        ticket.grace_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=settings.GRACE_PERIOD_SECONDS
        )
        await self.db.commit()

        # 2. Le client : téléphone (your_turn) puis SMS / push.
        await publish_event(ticket.org_id, {
            "type": EventType.YOUR_TURN.value,
            "ticket_id": ticket.id,
            "ticket_number": ticket_number,
            "agency_id": ticket.agency_id,
            "counter_name": counter_name,
            "message": f"Rappel : présentez-vous au {counter_name}",
            "timeout_seconds": settings.ABSENT_TIMEOUT_SECONDS,
            "recall": True,
        })
        await publish_event(ticket.org_id, {
            "type": EventType.TICKET_CALLED.value,
            "called_number": ticket_number,
            "counter_name": counter_name,
            "agency_id": ticket.agency_id,
            "recall": True,
        })

        try:
            from app.services.notification_service import NotificationService
            await NotificationService(self.db).notify_ticket_called(ticket, counter_name)
        except Exception as exc:  # noqa: BLE001 — jamais bloquant
            logger.error(f"Notification de relance échouée ({ticket_number}): {exc}")

        # 3. Les autres écrans du guichet (et ceux de l'agence) voient la relance.
        await self._publish_counter_activity(
            ticket, "ticket_recalled", counter_id=counter.id, counter_name=counter_name
        )

        # 4. Trace : qui a relancé, combien de fois, à quel guichet.
        await AuditService(self.db).log(
            org_id=ticket.org_id,
            action=AuditAction.TICKET_RECALLED,
            actor_id=actor.id,
            actor_role=actor.role.value,
            target_type="ticket",
            target_id=ticket.id,
            extra={
                "ticket_number": ticket_number,
                "counter_id": counter.id,
                "counter_name": counter_name,
                "recall_number": recall_count + 1,
            },
        )

        logger.info(
            f"Ticket relancé: {ticket_number} au {counter_name} "
            f"({recall_count + 1}/{settings.MAX_RECALLS_PER_TICKET}) par {actor.id}"
        )
        return {
            "success": True,
            "ticket_id": ticket.id,
            "ticket_number": ticket_number,
            "counter_name": counter_name,
            "recall_count": recall_count + 1,
            "recalls_left": settings.MAX_RECALLS_PER_TICKET - (recall_count + 1),
            "timeout_seconds": settings.ABSENT_TIMEOUT_SECONDS,
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
        await self._publish_queue_snapshot(ticket.org_id, ticket.agency_id, ticket.service_id)
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

        # Durée réelle de service (secondes) : source de l'EWMA AWTE
        if ticket.served_at:
            ticket.service_seconds = max(
                0, int((ticket.done_at - as_utc(ticket.served_at)).total_seconds())
            )

        await EtaEstimator(self.db).record_completed_ticket(ticket)
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
        await self._publish_queue_snapshot(ticket.org_id, ticket.agency_id, ticket.service_id)
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
        await EtaEstimator(self.db).record_interrupted_ticket(ticket, "incomplete")
        await self._release_counter(ticket)
        await self.db.commit()
        return {"success": True, "ticket_id": ticket.id}

    async def mark_absent(self, ticket_id: str, actor: User) -> dict:
        """
        Le client ne s'est pas présenté après timeout (manuel : par l'agent).
        Transition : CALLED → ABSENT

        Le délai de grâce (spécification §3.1) est vérifié ici : un agent ne peut
        pas déclarer absent un client qu'il vient d'appeler. Sans ce contrôle, le
        bouton « Client absent » était cliquable immédiatement après l'annonce —
        et un client qui traversait la salle se retrouvait derrière tout le monde
        pour avoir marché trop lentement.

        Un superviseur et au-dessus peut passer outre : c'est la dérogation prévue
        par la spécification, et elle est auditée avec son motif. Un agent, non —
        il n'a pas autorité pour raccourcir le délai qu'on lui impose.

        Raises:
            HTTPException 400: Transition interdite (ticket non CALLED)
            HTTPException 403: Ticket d'un autre guichet / d'une autre branch
            HTTPException 404: Ticket introuvable
            HTTPException 409: Délai de grâce non écoulé (agent)
        """
        ticket = await self._get_ticket_for_staff(ticket_id, actor)
        await self._assert_staff_can_handle(actor, ticket)

        deadline = self._grace_deadline(ticket)
        override = False
        seconds_left = 0
        if deadline is not None:
            now = datetime.now(timezone.utc)
            if now < deadline:
                seconds_left = int((deadline - now).total_seconds()) + 1
                if actor.role not in ANY_COUNTER_ROLES:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "code": "GRACE_PERIOD_NOT_ELAPSED",
                            "message": (
                                "Le client vient d'être appelé : attendez la fin du délai "
                                "de grâce avant de le déclarer absent."
                            ),
                            "seconds_left": seconds_left,
                            "grace_period_seconds": settings.GRACE_PERIOD_SECONDS,
                        },
                    )
                override = True
                logger.warning(
                    f"Dérogation : {actor.role.value} {actor.id} déclare absent "
                    f"{ticket.number} avec {seconds_left}s de grâce restantes"
                )
                await AuditService(self.db).log(
                    org_id=ticket.org_id,
                    action=AuditAction.TICKET_ABSENT_OVERRIDE,
                    actor_id=actor.id,
                    actor_role=actor.role.value,
                    target_type="ticket",
                    target_id=ticket.id,
                    extra={
                        "ticket_number": ticket.number,
                        "seconds_left": seconds_left,
                        "grace_period_seconds": settings.GRACE_PERIOD_SECONDS,
                    },
                )

        await self._set_absent(
            ticket, actor_id=actor.id, actor_role=actor.role.value,
            extra={"override_grace": True, "grace_seconds_left": seconds_left} if override else None,
        )
        logger.info(f"Ticket absent: {ticket.number}{' (dérogation)' if override else ''}")
        return {
            "success": True,
            "ticket_id": ticket.id,
            "missed_count": ticket.missed_count,
            "recovery_allowed": ticket.missed_count < settings.MAX_MISSED_COUNT,
            "recovery_expires_at": (
                ticket.recovery_expires_at.isoformat() if ticket.recovery_expires_at else None
            ),
        }

    def _grace_deadline(self, ticket: Ticket) -> datetime | None:
        """
        Fin du délai de grâce de l'appel en cours.

        `grace_expires_at` est la donnée de référence (migration 011). Les tickets
        appelés avant cette migration ne l'ont pas : on la reconstitue depuis
        `called_at`, qui est exactement la valeur qu'utilisait le code précédent.
        Retourne None si le ticket n'a jamais été appelé — auquel cas il n'y a
        rien à protéger, et la machine à états refusera la transition de toute façon.
        """
        if ticket.grace_expires_at is not None:
            return as_utc(ticket.grace_expires_at)
        if ticket.called_at is None:
            return None
        return as_utc(ticket.called_at) + timedelta(seconds=settings.GRACE_PERIOD_SECONDS)

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
        await EtaEstimator(self.db).record_interrupted_ticket(ticket, "transferred")
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
                sequence=ticket.queue_sequence,
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
            sequence=ticket.queue_sequence,
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
            sequence=ticket.queue_sequence,
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

        Règles :
          - WAITING  dont l'agence a FERMÉ (règle principale)         → CANCELLED + notification
          - WAITING  oublié depuis WAITING_TICKET_TTL_HOURS (filet)   → CANCELLED + notification
          - ABSENT   hors fenêtre de retour (ABSENT_RETURN_WINDOW)    → CANCELLED
          - SERVING  oublié par l'agent (SERVING_TICKET_TTL_HOURS)    → INCOMPLETE
          - INCOMPLETE non résolu (INCOMPLETE_TICKET_TTL_HOURS)       → CANCELLED

        L'expiration se fait à la fermeture de l'agence, pas sur une durée fixe :
        un ticket pris à 08:05 dans une agence ouverte 08:00–17:00 doit vivre
        jusqu'à 17:00, même s'il attend neuf heures. L'ancienne règle « 12 h »
        l'annulait à 20:05 sans prévenir — et surtout, sur une agence ouverte
        plus de douze heures, elle annulait des tickets en pleine journée.

        Returns:
            Compteurs par règle appliquée.
        """
        now = now or datetime.now(timezone.utc)
        counts = {"waiting": 0, "absent": 0, "serving": 0, "incomplete": 0}

        # Règle principale : plus personne ne peut être servi, l'agence est fermée.
        counts["waiting"] += await self._expire_waiting_of_closed_branches(now, batch)

        rules: list[tuple[str, TicketStatus, TicketStatus, datetime, object, list]] = [
            # Filet de sécurité : agence qui ne ferme jamais (horaires mal réglés).
            ("waiting", TicketStatus.WAITING, TicketStatus.CANCELLED,
             now - timedelta(hours=settings.WAITING_TICKET_TTL_HOURS), Ticket.created_at, []),
            # Fenêtre de récupération : DONNÉE persistée, ancrée sur l'ABSENCE
            # (migration 011). Avant, elle était recalculée depuis `called_at`,
            # donc décalée de 180 s et repoussée par tout nouvel appel.
            ("absent", TicketStatus.ABSENT, TicketStatus.CANCELLED,
             now, Ticket.recovery_expires_at, []),
            # Filet pour un ABSENT dont `recovery_expires_at` serait resté NULL
            # (ligne héritée d'avant la migration) : sans cette règle, il serait
            # immortel et bloquerait son client indéfiniment.
            ("absent", TicketStatus.ABSENT, TicketStatus.CANCELLED,
             now - timedelta(minutes=settings.ABSENT_RETURN_WINDOW_MINUTES), Ticket.called_at,
             [Ticket.recovery_expires_at.is_(None)]),
            ("serving", TicketStatus.SERVING, TicketStatus.INCOMPLETE,
             now - timedelta(hours=settings.SERVING_TICKET_TTL_HOURS), Ticket.served_at, []),
            ("incomplete", TicketStatus.INCOMPLETE, TicketStatus.CANCELLED,
             now - timedelta(hours=settings.INCOMPLETE_TICKET_TTL_HOURS), Ticket.served_at, []),
        ]

        for name, from_status, to_status, cutoff, column, extra_filters in rules:
            result = await self.db.execute(
                select(Ticket.id).where(
                    Ticket.status == from_status,
                    column.isnot(None),
                    column < cutoff,
                    *extra_filters,
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
                    if from_status == TicketStatus.WAITING:
                        await self._notify_expired(
                            ticket, "le délai d'attente maximal a été dépassé"
                        )
                    counts[name] += 1
                except Exception as exc:  # un ticket en erreur ne bloque pas les autres
                    await self.db.rollback()
                    logger.error(f"Expiration du ticket {ticket_id} échouée ({name}): {exc}")
        if any(counts.values()):
            logger.info(f"Tickets expirés: {counts}")
        return counts

    async def _expire_waiting_of_closed_branches(
        self, now: datetime, batch: int
    ) -> int:
        """
        Annule les tickets encore en file dont l'agence est fermée.

        Aucun nouveau ticket ne peut être créé hors horaires (`BRANCH_CLOSED`),
        donc tout ticket WAITING sur une agence fermée est un reste de la
        journée : personne ne viendra l'appeler.

        Returns:
            Nombre de tickets annulés.
        """
        now_local = now.astimezone(BURUNDI_TZ)
        branches = (await self.db.execute(
            select(Branch).where(Branch.is_active == True)  # noqa: E712
        )).scalars().all()
        closed_ids = [b.id for b in branches if not is_branch_open_now(b, now_local)]
        if not closed_ids:
            return 0

        rows = await self.db.execute(
            select(Ticket.id).where(
                Ticket.status == TicketStatus.WAITING,
                Ticket.agency_id.in_(closed_ids),
            ).limit(batch)
        )

        count = 0
        for ticket_id in rows.scalars().all():
            try:
                ticket = (await self.db.execute(
                    select(Ticket).where(Ticket.id == ticket_id).execution_options(populate_existing=True)
                )).scalar_one()
                await self._transition(
                    ticket, TicketStatus.CANCELLED, actor_role="system",
                    extra={"reason": "branch_closed"},
                )
                await self.db.commit()
                await self._leave_queue(ticket)
                await self._notify_expired(ticket, "l'agence a fermé")
                count += 1
            except Exception as exc:
                await self.db.rollback()
                logger.error(f"Clôture de fermeture échouée pour le ticket {ticket_id}: {exc}")
        return count

    async def _notify_expired(self, ticket: Ticket, reason: str) -> None:
        """Prévient le client qu'un ticket a été annulé, et pourquoi (non bloquant)."""
        try:
            await publish_event(ticket.org_id, {
                "type": EventType.TICKET_EXPIRED.value,
                "ticket_id": ticket.id,
                "ticket_number": ticket.number,
                "agency_id": ticket.agency_id,
                "reason": reason,
                "message": f"Votre ticket {ticket.number} a été annulé : {reason}.",
            })
        except Exception as exc:
            logger.error(f"Événement TICKET_EXPIRED non publié ({ticket.number}): {exc}")

        try:
            from app.services.notification_service import NotificationService

            await NotificationService(self.db).notify_ticket_expired(ticket, reason)
        except Exception as exc:
            logger.error(f"Notification d'annulation non envoyée ({ticket.number}): {exc}")

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
            Ticket.priority, Ticket.queue_sequence,
        ).where(Ticket.status == TicketStatus.WAITING)
        if org_id:
            query = query.where(Ticket.org_id == org_id)
        if agency_id:
            query = query.where(Ticket.agency_id == agency_id)
        if service_id:
            query = query.where(Ticket.service_id == service_id)

        restored = 0
        for tid, t_org, t_agency, t_service, t_priority, t_seq in (await self.db.execute(query)).all():
            score = queue_score(PRIORITY_SCORES[t_priority], t_seq)
            if await add_to_queue_if_absent(t_org, t_agency, t_service, tid, score):
                restored += 1
        return restored

    async def repair_open_branches(self) -> int:
        """
        Réinsère dans Redis les tickets `WAITING` des agences ouvertes qui manquent.

        Pourquoi ne pas appeler `rebuild_queue()` sans filtre (constat P1-11) :
        cette requête ne filtre que sur `status`, sans index dédié, et la table
        `tickets` grandit indéfiniment. Un scan complet toutes les minutes finit
        par coûter cher au bout de quelques mois — exactement le genre de dette
        qui n'apparaît qu'en production.

        On se limite donc aux agences **ouvertes et actives** : ce sont les
        seules dont les tickets `WAITING` ont un sens. Un ticket en attente dans
        une agence fermée est de toute façon expiré par
        `_expire_waiting_of_closed_branches`. Chaque `rebuild_queue` filtré
        s'appuie sur l'index `(agency_id, service_id, status)`.

        Returns:
            Nombre de tickets réinsérés.
        """
        branches = (
            await self.db.execute(
                select(Branch.org_id, Branch.id).where(
                    Branch.is_open.is_(True),
                    Branch.is_active.is_(True),
                )
            )
        ).all()

        restored = 0
        for org_id, branch_id in branches:
            restored += await self.rebuild_queue(org_id=org_id, agency_id=branch_id)
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
            service = await self._get_service_row(service_id, org_id)
            avg_seconds = await self._avg_service_seconds(
                service_id, service.avg_duration_minutes if service else 0
            ) if service is not None else 0
            estimated_waits: dict[str, int | None] = {}
            eta_confidences: dict[str, str] = {}
            eta_calculated_at = None
            # Non-null marks the production payload, even if the estimate is
            # unavailable; WebSocket clients must not fall back to a fake ETA.
            eta_version = 0
            eta_available = False
            if service is not None:
                try:
                    estimate = (await EtaEstimator(self.db).estimate_many(
                        [service], persist_forecasts=True
                    )).get(service_id)
                    if estimate is not None:
                        estimated_waits = {
                            ticket_id: (
                                math.ceil(value.wait_seconds / 60)
                                if value.wait_seconds is not None else None
                            )
                            for ticket_id, value in estimate.ticket_details.items()
                        }
                        eta_confidences = {
                            ticket_id: value.confidence
                            for ticket_id, value in estimate.ticket_details.items()
                        }
                        eta_calculated_at = estimate.calculated_at.isoformat() if estimate.calculated_at else None
                        eta_version = estimate.version
                        eta_available = estimate.next_ticket_minutes is not None
                except Exception as exc:  # noqa: BLE001 — conserver le snapshot historique
                    logger.warning(
                        f"ETA dynamique indisponible pour le snapshot {service_id} ({exc})"
                    )
            event = queue_snapshot_event(
                agency_id=agency_id,
                service_id=service_id,
                positions={tid: index + 1 for index, tid in enumerate(ids)},
                current_called_number=await self._current_called_number(service_id, org_id),
                eta_seconds_per_position=avg_seconds,
                estimated_wait_minutes_by_ticket=estimated_waits,
                eta_confidence_by_ticket=eta_confidences,
                eta_calculated_at=eta_calculated_at,
                eta_version=eta_version,
                eta_available=eta_available,
            )
            # Commit the forecast projection and its event together. The business
            # transition was already committed by the Queue Engine; from this point
            # onward the existing outbox guarantees replay after Redis recovery.
            outbox = OutboxService(self.db)
            row_id = await outbox.enqueue(
                org_id=org_id,
                event_type=event["type"],
                payload=event,
            )
            await self.db.commit()
            if await outbox.publish_now(row_id, org_id, event):
                await self.db.commit()
        except Exception as exc:  # noqa: BLE001 — jamais bloquant
            logger.error(f"Snapshot de file non publié (service={service_id}): {exc}")

    # ══ ETA ════════════════════════════════════════════════════════════════════

    async def estimate_service_queues(
        self, services: list[Service]
    ) -> dict[str, ServiceQueueEstimate]:
        """Retourne les ETA dynamiques groupées, avec état indisponible explicite."""
        return await EtaEstimator(self.db).estimate_many(services)

    async def estimate_ticket_wait_minutes(
        self,
        ticket: Ticket,
        service: Service | None,
        *,
        persist_forecast: bool = False,
    ) -> int | None:
        """Prévoit l'attente restante à partir de l'état opérationnel courant.

        Une absence de capacité opérationnelle ou une erreur temporaire de calcul
        renvoie `None` : la position seule ne décrit pas la capacité disponible.
        """
        if ticket.status != TicketStatus.WAITING or service is None:
            return ticket.estimated_wait_minutes or 0
        try:
            estimate = (await EtaEstimator(self.db).estimate_many(
                [service], persist_forecasts=persist_forecast
            )).get(service.id)
            if estimate is not None:
                return estimate.ticket_minutes.get(ticket.id)
        except Exception as exc:  # noqa: BLE001 — ETA ne doit jamais casser le ticket
            logger.warning(
                f"ETA dynamique indisponible pour {ticket.number} ({exc}); aucune prévision n'est publiée"
            )
            return None

    async def _avg_service_seconds(self, service_id: str | None, default_minutes: int) -> int:
        """Lit l'EWMA persisté; tant que le seuil n'est pas atteint, utilise la config."""
        if not service_id:
            return max(0, default_minutes) * 60
        service = (await self.db.execute(
            select(Service).where(Service.id == service_id)
        )).scalar_one_or_none()
        if service is None:
            return max(0, default_minutes) * 60
        from app.models.eta import EtaServiceProfile
        profile = (await self.db.execute(
            select(EtaServiceProfile).where(
                EtaServiceProfile.org_id == service.org_id,
                EtaServiceProfile.service_id == service_id,
            )
        )).scalar_one_or_none()
        if profile and profile.sample_count >= service.eta_min_service_samples:
            return max(0, int(round(profile.ewma_seconds)))
        return max(0, service.avg_duration_minutes) * 60

    async def avg_service_seconds_batch(self, services: list[Service]) -> dict[str, int]:
        """
        Variante GROUPÉE de `_avg_service_seconds` : une requête pour N services.

        `_avg_service_seconds` interroge la base une fois par service. Sur l'écran
        de salle — appelé toutes les 7 s par chaque poste de l'agence — cela fait
        N requêtes par appel et par poste. La fenêtre glissante est reconstruite
        avec `row_number()` (SQLite ≥ 3.25 et PostgreSQL savent le faire), ce qui
        ramène le coût à une seule requête quel que soit le nombre de services.

        Args:
            services: Services à évaluer (chacun fournit sa durée par défaut)

        Returns:
            dict service_id → durée moyenne en secondes
        """
        if not services:
            return {}

        ids = [service.id for service in services]
        org_ids = {service.org_id for service in services if service.org_id}
        profiles = (await self.db.execute(
            select(EtaServiceProfile).where(
                EtaServiceProfile.org_id.in_(org_ids),
                EtaServiceProfile.service_id.in_(ids),
            )
        )).scalars().all() if org_ids else []
        by_id = {profile.service_id: profile for profile in profiles}
        return {
            service.id: max(0, int(round(by_id[service.id].ewma_seconds)))
            if service.id in by_id and by_id[service.id].sample_count >= service.eta_min_service_samples
            else max(0, service.avg_duration_minutes) * 60
            for service in services
        }

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

        # Les écrans de guichet de l'agence apprennent le changement en temps réel.
        # Diffuser ici plutôt que chez chaque appelant garantit qu'AUCUNE
        # transition n'est oubliée — c'est le point unique par lequel passe tout
        # changement d'état. Contrepartie assumée : une transition ensuite
        # annulée par un rollback produit un événement de trop. Le client y réagit
        # en relisant l'état, donc l'annonce de trop est sans effet ; une
        # transition MANQUÉE, elle, laisserait le poste muet jusqu'au prochain
        # sondage — c'est le défaut qu'on corrige.
        await self._publish_counter_activity(
            ticket, new_status.value, counter_id=counter_id, counter_name=counter_name
        )

    async def _publish_counter_activity(
        self,
        ticket: Ticket,
        reason: str,
        *,
        status_value: str | None = None,
        counter_id: str | None = None,
        counter_name: str | None = None,
    ) -> None:
        """
        Prévient les écrans de guichet qu'une file de l'agence a changé.

        Depuis la migration 014, l'événement passe par l'OUTBOX : il est écrit
        dans la transaction courante (donc commité avec le changement d'état),
        puis diffusé immédiatement. Si la diffusion échoue, la ligne reste en
        attente et le scheduler la rejoue — un écran de salle ne peut plus
        manquer un appel parce que Redis est tombé une seconde.

        La diffusion immédiate, elle, ne lève jamais : le temps réel est un
        accélérateur, pas une dépendance du chemin métier.
        """
        event = counter_activity_event(
            agency_id=ticket.agency_id,
            service_id=ticket.service_id,
            reason=reason,
            ticket_number=ticket.number,
            status=status_value or ticket.status.value,
            counter_id=counter_id or ticket.counter_id,
            counter_name=counter_name or ticket.counter_name,
        )

        # L'écriture en base fait partie de la transaction métier : si elle
        # échoue, l'opération échoue — c'est voulu, c'est la durabilité.
        row_id = await OutboxService(self.db).enqueue(
            org_id=ticket.org_id,
            event_type=event.get("type", reason),
            payload=event,
        )

        try:
            await OutboxService(self.db).publish_now(row_id, ticket.org_id, event)
        except Exception as exc:  # noqa: BLE001 — jamais bloquant
            # `publish_event` retourne False plutôt que de lever ; ce filet
            # couvre l'imprévu (et laisse la ligne en attente pour le rejeu).
            logger.error(f"Événement guichet non publié ({ticket.number}): {exc}")

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
                        # Délai de grâce : pendant ce temps, l'agent ne peut pas
                        # encore déclarer le client absent (il vient de l'appeler).
                        # Persisté et non recalculé, pour que l'échéance reste
                        # vérifiable même après un redémarrage ou une reprise.
                        grace_expires_at=now + timedelta(seconds=settings.GRACE_PERIOD_SECONDS),
                        # Un ticket appelé n'est plus en récupération : l'échéance
                        # de reprise est effacée, une future absence en reposera une.
                        recovery_expires_at=None,
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
                await EtaEstimator(self.db).record_prediction_at_call(ticket, now)
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

    async def _set_absent(
        self,
        ticket: Ticket,
        *,
        actor_id: str | None,
        actor_role: str,
        extra: dict | None = None,
    ) -> None:
        """CALLED → ABSENT : libère le guichet, annule les échéances, notifie le client."""
        # Les trois valeurs qui portent l'algorithme d'absence. Elles étaient
        # recalculées à la volée depuis `called_at` : la fenêtre de récupération
        # partait donc de l'APPEL (180 s d'écart), et un nouvel appel écrasait
        # `called_at`, repoussant silencieusement l'échéance. Une échéance est une
        # donnée, pas un calcul refait à chaque lecture.
        now = datetime.now(timezone.utc)
        missed_count = (ticket.missed_count or 0) + 1
        recovery_expires_at = now + timedelta(minutes=settings.ABSENT_RETURN_WINDOW_MINUTES)
        ticket.missed_count = missed_count
        ticket.last_missed_at = now
        ticket.recovery_expires_at = recovery_expires_at
        ticket.grace_expires_at = None  # la grâce concerne l'appel, pas l'absence

        await self._transition(
            ticket, TicketStatus.ABSENT, actor_id=actor_id, actor_role=actor_role,
            extra={
                "missed_count": missed_count,
                "recovery_expires_at": recovery_expires_at.isoformat(),
                "recovery_allowed": missed_count < settings.MAX_MISSED_COUNT,
                **(extra or {}),
            },
        )
        await EtaEstimator(self.db).record_interrupted_ticket(ticket, "absent")
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
            # Le client doit savoir s'il lui reste une chance : au-delà du
            # plafond, revenir est refusé et il devra prendre un nouveau ticket.
            "missed_count": missed_count,
            "recovery_allowed": missed_count < settings.MAX_MISSED_COUNT,
            "recovery_expires_at": recovery_expires_at.isoformat(),
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

        Dégradation : si Redis est indisponible, la position exacte est inconnue mais
        la lecture ne doit PAS échouer. On renvoie alors la position 0 (inconnue) et
        le nombre d'attente lu en base. L'AWTE calcule son estimation depuis l'état
        PostgreSQL et renvoie null si elle ne peut pas établir une capacité fiable.
        """
        position = 0
        total = 0
        eta = ticket.estimated_wait_minutes
        service = await self._get_service_row(ticket.service_id, ticket.org_id)

        if ticket.status == TicketStatus.WAITING:
            try:
                position = await get_queue_position(
                    ticket.org_id, ticket.agency_id, ticket.service_id, ticket.id
                )
                if position == 0:
                    position = await add_to_queue(
                        ticket.org_id, ticket.agency_id, ticket.service_id, ticket.id,
                        PRIORITY_SCORES[ticket.priority],
                        sequence=ticket.queue_sequence,
                    )
                    logger.warning(f"Ticket {ticket.number} absent de Redis : remis en file (auto-réparation)")
                total = await get_queue_size(ticket.org_id, ticket.agency_id, ticket.service_id)
                eta = await self.estimate_ticket_wait_minutes(
                    ticket,
                    service,
                    persist_forecast=True,
                )
            except Exception as exc:  # noqa: BLE001 — Redis ne doit jamais casser une lecture
                logger.warning(
                    f"File indisponible pour {ticket.number} ({exc}) : position inconnue, "
                    "repli sur la base"
                )
                position = 0
                total = await self._count_waiting_in_db(ticket.service_id)
                eta = await self.estimate_ticket_wait_minutes(
                    ticket, service,
                    persist_forecast=True,
                )

        response = self._to_response(ticket, position=max(0, position), total=total, eta=eta)
        if ticket.status == TicketStatus.WAITING:
            await self.db.commit()
        await self._fill_eta_metadata(response, ticket)
        await self._fill_context_names(response, ticket.service_id, ticket.org_id)
        return response

    async def _fill_eta_metadata(self, response: TicketResponse, ticket: Ticket) -> None:
        """Attach current forecast metadata from the tenant-scoped projection."""
        if not ticket.org_id:
            return
        prediction = (await self.db.execute(
            select(EtaPrediction)
            .where(
                EtaPrediction.ticket_id == ticket.id,
                EtaPrediction.org_id == ticket.org_id,
                EtaPrediction.branch_id == ticket.agency_id,
                EtaPrediction.service_id == ticket.service_id,
            )
            .order_by(EtaPrediction.version.desc())
            .limit(1)
        )).scalar_one_or_none()
        if prediction is None:
            return
        response.estimated_wait_confidence = prediction.confidence
        response.eta_calculated_at = prediction.calculated_at
        response.eta_version = prediction.version

    async def _count_waiting_in_db(self, service_id: str) -> int:
        """Nombre de tickets WAITING d'un service, lu en base (repli quand Redis est absent)."""
        try:
            return (await self.db.execute(
                select(func.count(Ticket.id)).where(
                    Ticket.service_id == service_id,
                    Ticket.status == TicketStatus.WAITING,
                )
            )).scalar() or 0
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Comptage d'attente en base impossible (service={service_id}): {exc}")
            return 0

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
            called_at=ticket.called_at,
            served_at=ticket.served_at,
            done_at=ticket.done_at,
            grace_expires_at=ticket.grace_expires_at,
            recovery_expires_at=ticket.recovery_expires_at,
            missed_count=ticket.missed_count or 0,
            queue_sequence=ticket.queue_sequence,
        )

    async def _current_called_number(self, service_id: str, org_id: str | None = None) -> str | None:
        """Dernier numéro appelé dans un service (affiché dans la salle d'attente)."""
        query = select(Ticket.number).where(
            Ticket.service_id == service_id, Ticket.called_at.isnot(None)
        )
        if org_id:
            query = query.where(Ticket.org_id == org_id)
        result = await self.db.execute(query.order_by(Ticket.called_at.desc()).limit(1))
        return result.scalar_one_or_none()

    async def current_called_numbers(
        self, service_ids: list[str], org_id: str | None = None
    ) -> dict[str, str | None]:
        """
        Variante GROUPÉE de `_current_called_number` : une requête pour N services.

        Le dernier numéro appelé est celui de la ligne de rang 1 dans chaque
        service, triée par `called_at` décroissant — donc une fenêtre
        `row_number()` plutôt qu'un `LIMIT 1` par service.

        Args:
            service_ids: Services à interroger

        Returns:
            dict service_id → numéro appelé (None si le service n'a rien appelé)
        """
        if not service_ids:
            return {}

        ranked_query = (
            select(
                Ticket.service_id.label("service_id"),
                Ticket.number.label("number"),
                func.row_number().over(
                    partition_by=Ticket.service_id,
                    order_by=Ticket.called_at.desc(),
                ).label("rn"),
            )
            .where(Ticket.service_id.in_(service_ids), Ticket.called_at.isnot(None))
        )
        if org_id:
            ranked_query = ranked_query.where(Ticket.org_id == org_id)
        ranked = ranked_query.subquery()

        rows = (await self.db.execute(
            select(ranked.c.service_id, ranked.c.number).where(ranked.c.rn == 1)
        )).all()

        numbers: dict[str, str | None] = {service_id: None for service_id in service_ids}
        numbers.update({service_id: number for service_id, number in rows})
        return numbers

    async def _get_service_row(self, service_id: str, org_id: str | None = None) -> Service | None:
        """Récupère un service dans son tenant lorsque celui-ci est connu."""
        query = select(Service).where(Service.id == service_id)
        if org_id:
            query = query.where(Service.org_id == org_id)
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    async def _fill_context_names(
        self, response: TicketResponse, service_id: str, org_id: str | None = None
    ) -> None:
        """
        Renseigne les noms lisibles (organisation, agence, service) d'une réponse.

        Une seule requête, en LEFT JOIN : `services.org_id` et `branches.org_id`
        sont nullables. Un INNER JOIN ferait disparaître les tickets rattachés à
        une agence sans organisation — cas hérité, mais qui existe en base.

        N'échoue jamais : un nom manquant dégrade l'affichage (le mobile retombe
        sur son cache local), il ne doit pas empêcher la consultation du ticket.
        """
        try:
            query = (
                select(
                    Service.name,
                    Branch.name,
                    Organization.id,
                    Organization.name,
                )
                .select_from(Service)
                .outerjoin(Branch, Branch.id == Service.branch_id)
                .outerjoin(Organization, Organization.id == Branch.org_id)
                .where(Service.id == service_id)
            )
            if org_id:
                query = query.where(Branch.org_id == org_id)
            row = (await self.db.execute(query)).first()
        except Exception as exc:  # noqa: BLE001 — l'affichage ne casse pas la lecture
            logger.warning(
                f"Noms d'affichage indisponibles pour le service {service_id} : {exc}"
            )
            return
        if row is None:
            return
        service_name, branch_name, org_id, org_name = row
        response.service_name = service_name
        response.agency_name = branch_name
        response.organization_id = org_id
        response.organization_name = org_name

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

    async def _next_queue_sequence(self) -> int:
        """
        Numéro d'ordre de file monotone et GLOBAL (migration 013).

        PostgreSQL : `nextval` sur une séquence dédiée. C'est atomique, sans
        verrou applicatif et sans risque de doublon, même avec plusieurs
        instances — deux transactions concurrentes obtiennent deux valeurs
        distinctes, dans l'ordre de leur appel. Les trous (transaction annulée)
        sont normaux et sans conséquence : seule la croissance compte.

        SQLite (tests) : `MAX + 1`. Suffisant parce que les tests sont
        mono-écrivain ; ce n'est PAS le chemin de production.

        Returns:
            Le prochain numéro d'ordre, strictement supérieur à tous les
            précédents.
        """
        dialect = self.db.bind.dialect.name if self.db.bind is not None else "postgresql"
        if dialect == "postgresql":
            result = await self.db.execute(text("SELECT nextval('ticket_queue_sequence_seq')"))
            return int(result.scalar_one())

        maximum = (await self.db.execute(select(func.max(Ticket.queue_sequence)))).scalar() or 0
        return int(maximum) + 1

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

        try:
            return await next_ticket_sequence(agency_id, prefix, day, load_seed)
        except Exception as exc:  # noqa: BLE001
            # Panne Redis : la numérotation se replie sur la base. Sans ce repli,
            # `create_ticket` restait dépendant de Redis même après la migration
            # 012 — l'index unique protégeait l'invariant, mais le client ne
            # pouvait toujours pas obtenir de ticket.
            #
            # Deux créations STRICTEMENT simultanées dans la même agence pendant
            # une panne peuvent obtenir le même numéro : c'est un désagrément
            # d'affichage (le ticket reste unique par son `id`), pas une
            # corruption, et il disparaît dès que Redis répond. Refuser la
            # création serait pire : l'agence ne délivrerait plus aucun ticket.
            logger.error(
                f"Compteur de numéros indisponible (Redis) pour {prefix} : "
                f"repli sur la base : {exc}"
            )
            return await load_seed() + 1

    async def _get_callable_counter(
        self, counter_id: str, agency_id: str, org_id: str, *, for_update: bool = False
    ) -> Counter:
        """
        Récupère un guichet depuis lequel on peut appeler un ticket.

        Le guichet doit appartenir à l'organisation ET à la branch demandée,
        être actif et ouvert.

        Args:
            counter_id: Guichet visé
            agency_id: Branch attendue
            org_id: Organisation attendue
            for_update: Pose un verrou de ligne (SELECT … FOR UPDATE) pour sérialiser
                        deux appels simultanés depuis le même guichet. Sans effet
                        sur SQLite (tests), qui ne connaît pas cette clause.

        Raises:
            HTTPException 404: Guichet introuvable dans cette org/branch
            HTTPException 400: Guichet fermé
        """
        query = select(Counter).where(
            Counter.id == counter_id,
            Counter.org_id == org_id,
            Counter.agency_id == agency_id,
            Counter.is_active == True,  # noqa: E712
        )
        if for_update:
            query = query.with_for_update()
        result = await self.db.execute(query)
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
        if counter.is_paused:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "COUNTER_PAUSED", "message": "Ce guichet est en pause"},
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

    async def _assert_counter_free(self, counter: Counter) -> None:
        """
        Refuse un nouvel appel tant que le guichet traite encore un client.

        `Counter.current_ticket_id` est la seule trace du client en cours au guichet.
        S'il pointe sur un ticket CALLED ou SERVING, appeler le suivant laisserait ce
        client sans guichet : il resterait CALLED et serait déclaré ABSENT par le
        scheduler au bout de ABSENT_TIMEOUT_SECONDS, alors qu'il est présent.

        Un `current_ticket_id` périmé (ticket terminé, annulé, transféré) ne bloque
        pas : le statut réel est relu en base, jamais supposé.

        Raises:
            HTTPException 409: Le guichet traite déjà un ticket
        """
        if not counter.current_ticket_id:
            return

        row = (await self.db.execute(
            select(Ticket.number, Ticket.status).where(Ticket.id == counter.current_ticket_id)
        )).first()
        if row is None:
            return

        number, ticket_status = row
        if ticket_status not in (TicketStatus.CALLED, TicketStatus.SERVING):
            return

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "COUNTER_BUSY",
                "message": (
                    f"Le guichet traite déjà {number}. Terminez ce ticket, "
                    "marquez-le absent ou transférez-le avant d'appeler le suivant."
                ),
                "ticket_number": number,
                "ticket_status": ticket_status.value,
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
