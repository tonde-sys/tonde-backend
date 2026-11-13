"""
Service Tickets — Queue Engine de TONDE.

Responsabilités :
  - Création et suivi des tickets
  - Machine à états explicite (transitions contrôlées)
  - Calcul ETA
  - Intégration Redis (file d'attente, position)
  - Isolation multi-tenant via org_id
  - Audit trail via QueueLog (source de données Analytics)

Toute modification de ce fichier doit être analysée
avec soin : c'est le cœur du système.
"""
import logging
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException, status

from app.models.ticket import (
    Ticket, TicketStatus, TicketPriority,
    PRIORITY_SCORES, ALLOWED_TRANSITIONS,
)
from app.models.branch import Branch, Service
from app.models.queue_log import QueueLog, QueueLogAction
from app.models.user import UserRole
from app.core.redis import (
    add_to_queue, get_queue_position,
    get_queue_size, remove_from_queue, get_next_ticket,
    next_daily_sequence,
)
from app.schemas.ticket import CreateTicketRequest, TicketResponse, TicketHistoryResponse

logger = logging.getLogger(__name__)

# Statuts considérés comme "actifs" — bloquent la création d'un nouveau ticket.
# Règle MVP DÉCISION 1 : 1 seul ticket actif par utilisateur sur TOUTE la plateforme.
#
# ATTENTION : ne lister ici QUE des statuts dont on peut sortir.
# TRANSFERRED et INCOMPLETE sont des états TERMINAUX (ALLOWED_TRANSITIONS = []).
# Les inclure bloquait définitivement le client : impossible de reprendre un
# ticket après un transfert ou un service interrompu, sans aucune échappatoire.
ACTIVE_STATUSES: list[TicketStatus] = [
    TicketStatus.WAITING,
    TicketStatus.CALLED,
    TicketStatus.SERVING,
    TicketStatus.ABSENT,
]

# Mapping status → QueueLogAction pour l'audit trail
STATUS_TO_LOG_ACTION: dict[TicketStatus, QueueLogAction] = {
    TicketStatus.CALLED:      QueueLogAction.TICKET_CALLED,
    TicketStatus.SERVING:     QueueLogAction.TICKET_SERVING,
    TicketStatus.DONE:        QueueLogAction.TICKET_DONE,
    TicketStatus.ABSENT:      QueueLogAction.TICKET_ABSENT,
    TicketStatus.WAITING:     QueueLogAction.TICKET_RETURNED,   # retour ABSENT → WAITING
    TicketStatus.TRANSFERRED: QueueLogAction.TICKET_TRANSFERRED,
    TicketStatus.CANCELLED:   QueueLogAction.TICKET_CANCELLED,
    TicketStatus.INCOMPLETE:  QueueLogAction.TICKET_INCOMPLETE,
}

# Priorités qu'un client peut s'attribuer lui-même.
# PRIORITY (femme enceinte, personne âgée, handicap) est une déclaration
# socialement vérifiable à l'accueil ; VIP et EMERGENCY sont des privilèges
# payants ou médicaux qui doivent être attribués par le staff.
CLIENT_ALLOWED_PRIORITIES: frozenset[TicketPriority] = frozenset({
    TicketPriority.STANDARD,
    TicketPriority.PRIORITY,
})


class TicketService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── Créer un nouveau ticket ───────────────────────────────────────────────
    async def create_ticket(
        self,
        data: CreateTicketRequest,
        user_id: str,
        org_id: str | None,
        actor_role: UserRole | None = None,
    ) -> TicketResponse:
        """
        Crée un ticket et l'insère dans la file d'attente Redis.

        Args:
            data: Données de création (agency_id, service_id, priority)
            user_id: ID de l'utilisateur connecté
            org_id: ID de l'organisation (isolation multi-tenant)
                    Peut être None pour les clients OTP sans affiliation
            actor_role: Rôle de l'appelant. Détermine les priorités autorisées.
                        None est traité comme CLIENT (refus par défaut).

        Returns:
            TicketResponse avec numéro, position, ETA et QR token

        Raises:
            HTTPException 400: Agence fermée ou données invalides
            HTTPException 403: Agence d'une autre org, ou priorité non autorisée
            HTTPException 409: Ticket actif déjà existant
        """
        # Résoudre la priorité EN PREMIER — contrôle par rôle obligatoire.
        # Sans ce garde-fou, n'importe quel client pouvait se déclarer
        # "emergency" et passer devant toute la file. Fail-fast : on refuse
        # avant toute lecture DB inutile.
        priority = self._resolve_priority(data.priority, actor_role)
        priority_score = PRIORITY_SCORES[priority]

        # Récupérer la branch — si org_id est None, on cherche globalement
        # puis on utilise son org_id pour le reste
        branch = await self._get_agency_for_creation(data.agency_id, org_id)

        # Si le client n'a pas d'org, on utilise l'org de la branch
        ticket_org_id = org_id if org_id else branch.org_id

        if not branch.is_active or not branch.is_open:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "BRANCH_CLOSED", "message": "Cette branch est fermée en ce moment"},
            )

        # Vérifier que le service existe dans cette agence
        service = await self._get_service(data.service_id, data.agency_id, ticket_org_id)

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

        # ── Création atomique du ticket + son audit trail ────────────────────
        # Un seul commit : soit le ticket ET sa ligne QueueLog existent, soit
        # rien. Avant, trois commit() successifs pouvaient laisser un ticket
        # sans trace d'audit si le process mourait entre deux.
        prefix = service.ticket_prefix          # capturé avant tout rollback
        avg_service_minutes = branch.avg_service_minutes
        redis_org_id = ticket_org_id or "public"
        ticket = None

        for attempt in range(2):
            sequence = await self._get_next_sequence(data.agency_id, prefix)
            candidate = Ticket(
                number=f"{prefix}-{sequence}",
                prefix=prefix,
                sequence=sequence,
                user_id=user_id,
                org_id=ticket_org_id,
                agency_id=data.agency_id,
                service_id=data.service_id,
                priority=priority,
                status=TicketStatus.WAITING,
            )
            self.db.add(candidate)
            try:
                await self.db.flush()   # obtenir candidate.id avant commit
            except IntegrityError:
                # Filet de sécurité : l'index unique (agency, prefix, sequence,
                # jour) a détecté un doublon malgré le compteur Redis.
                await self.db.rollback()
                if attempt == 0:
                    logger.warning(
                        f"Numéro de ticket en collision (tentative {attempt + 1}), "
                        f"nouvelle séquence pour {prefix} @ {data.agency_id}"
                    )
                    continue
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "code": "TICKET_NUMBER_CONFLICT",
                        "message": "Impossible d'attribuer un numéro de ticket. Réessayez dans un instant.",
                    },
                )
            ticket = candidate
            break

        # Audit trail dans la MÊME transaction que le ticket
        await self._log_action(
            ticket=ticket,
            action=QueueLogAction.TICKET_CREATED,
            from_status=None,
            to_status=TicketStatus.WAITING,
            elapsed_seconds=0,
        )
        await self.db.commit()
        await self.db.refresh(ticket)

        # ── Insertion dans la file Redis (état dérivé, non transactionnel) ───
        # Si Redis échoue, le ticket existerait en base mais dans aucune file :
        # le client attendrait indéfiniment un appel qui ne viendrait jamais.
        # On compense donc en annulant le ticket avant de remonter l'erreur.
        try:
            position = await add_to_queue(
                redis_org_id, data.agency_id, data.service_id, ticket.id, priority_score
            )
            total = await get_queue_size(redis_org_id, data.agency_id, data.service_id)
        except Exception as e:
            logger.critical(
                f"File Redis indisponible — annulation du ticket {ticket.number}: {e}",
                exc_info=True,
            )
            await self._cancel_ghost_ticket(ticket)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "QUEUE_UNAVAILABLE",
                    "message": (
                        "Le service de file d'attente est momentanément indisponible. "
                        "Aucun ticket n'a été créé. Réessayez dans quelques instants."
                    ),
                },
            )

        # Calculer l'ETA (donnée dérivée — un échec ici n'invalide pas le ticket)
        eta = max(0, (position - 1) * avg_service_minutes)
        try:
            ticket.estimated_wait_minutes = eta
            await self.db.commit()
        except Exception as e:
            logger.warning(f"ETA non persistée pour {ticket.number}: {e}")

        logger.info(f"Ticket créé: {ticket.number} | org={ticket_org_id} | position={position} | ETA={eta}min")

        return TicketResponse(
            id=ticket.id,
            number=ticket.number,
            status=ticket.status.value,
            priority=ticket.priority.value,
            agency_id=ticket.agency_id,
            service_id=ticket.service_id,
            qr_token=ticket.qr_token,
            position=position,
            total_in_queue=total,
            estimated_wait_minutes=eta,
            created_at=ticket.created_at,
        )

    async def _cancel_ghost_ticket(self, ticket: Ticket) -> None:
        """
        Annule un ticket qui n'a pas pu être inséré dans la file Redis.

        Compense une création partielle : sans cela, le ticket resterait
        WAITING en base pour toujours, invisible de tous les guichets.
        Best-effort : on ne veut pas masquer l'erreur d'origine.

        Args:
            ticket: Le ticket à annuler
        """
        try:
            ticket.status = TicketStatus.CANCELLED
            await self._log_action(
                ticket=ticket,
                action=QueueLogAction.TICKET_CANCELLED,
                from_status=TicketStatus.WAITING,
                to_status=TicketStatus.CANCELLED,
            )
            await self.db.commit()
            logger.warning(f"Ticket fantôme annulé: {ticket.number}")
        except Exception as e:
            logger.critical(
                f"Échec de l'annulation du ticket fantôme {ticket.id}: {e}",
                exc_info=True,
            )

    # ── Obtenir le statut d'un ticket ─────────────────────────────────────────
    async def get_ticket(
        self,
        ticket_id: str,
        user_id: str,
        org_id: str | None,
        actor_role: UserRole | None = None,
    ) -> TicketResponse:
        """
        Retourne les informations actuelles d'un ticket.

        Sécurité :
          - Un CLIENT ne voit que ses propres tickets.
          - Le STAFF (agent, superviseur, admin) voit tous les tickets de son
            organisation — indispensable au desktop guichetier, qui doit
            retrouver le ticket qu'il vient d'appeler.
          - org_id garantit l'isolation multi-tenant dans les deux cas.

        Args:
            ticket_id: ID du ticket
            user_id: ID de l'utilisateur connecté
            org_id: Organisation de l'utilisateur. None pour un client OTP
                    (sans organisation) : le ticket est alors retrouvé par
                    propriétaire, et l'org du ticket sert pour la suite.
            actor_role: Rôle de l'appelant. None est traité comme CLIENT.

        Raises:
            HTTPException 403: Ticket d'un autre utilisateur (client uniquement)
            HTTPException 404: Ticket introuvable ou hors organisation
        """
        is_staff = actor_role is not None and actor_role != UserRole.CLIENT

        if org_id is None:
            ticket = await self._get_ticket_for_owner(ticket_id, user_id)
            org_id = ticket.org_id
        else:
            ticket = await self._get_ticket_by_id(ticket_id, org_id)

        # Le staff agit dans le périmètre de son organisation (déjà filtré par
        # _get_ticket_by_id). Seul un client est restreint à ses propres tickets.
        if not is_staff and ticket.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "FORBIDDEN", "message": "Accès non autorisé à ce ticket"},
            )

        position = await get_queue_position(org_id, ticket.agency_id, ticket.service_id, ticket.id)
        total = await get_queue_size(org_id, ticket.agency_id, ticket.service_id)

        branch_result = await self.db.execute(
            select(Branch).where(Branch.id == ticket.agency_id, Branch.org_id == org_id)
        )
        branch = branch_result.scalar_one_or_none()
        eta = max(0, (position - 1) * (branch.avg_service_minutes if branch else 5))

        return TicketResponse(
            id=ticket.id,
            number=ticket.number,
            status=ticket.status.value,
            priority=ticket.priority.value,
            agency_id=ticket.agency_id,
            service_id=ticket.service_id,
            qr_token=ticket.qr_token,
            position=position if position > 0 else 0,
            total_in_queue=total,
            estimated_wait_minutes=eta,
            created_at=ticket.created_at,
            counter_name=ticket.counter_name,
        )

    # ── Annuler un ticket ─────────────────────────────────────────────────────
    async def cancel_ticket(
        self, ticket_id: str, user_id: str, org_id: str | None
    ) -> dict:
        """
        Le client annule son ticket.
        Seuls les tickets WAITING ou ABSENT peuvent être annulés par le client.

        ABSENT est annulable : sans cette sortie, un client marqué absent
        restait bloqué (ABSENT bloque la création d'un nouveau ticket) sans
        autre option que de revenir en file d'attente.

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
            org_id = ticket.org_id
        else:
            ticket = await self._get_ticket_by_id(ticket_id, org_id)

        if ticket.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "FORBIDDEN", "message": "Accès non autorisé"},
            )

        await self._transition(ticket, TicketStatus.CANCELLED)
        await remove_from_queue(org_id, ticket.agency_id, ticket.service_id, ticket.id)
        await self.db.commit()

        logger.info(f"Ticket annulé: {ticket.number} | user={user_id}")
        return {"success": True, "message": "Ticket annulé"}

    # ── Appeler le prochain ticket (guichetier) ───────────────────────────────
    async def call_next(
        self, agency_id: str, service_id: str, counter_id: str, counter_name: str, org_id: str
    ) -> dict:
        """
        Le guichetier appelle le prochain ticket de la file du service.

        Args:
            agency_id: ID de l'agence du guichetier
            service_id: ID du service dont appeler le prochain ticket (DÉCISION 5)
            counter_id: ID du guichet appelant
            counter_name: Nom affiché sur l'écran salle d'attente
            org_id: ID de l'organisation (isolation multi-tenant)

        Returns:
            Dict avec ticket_id, ticket_number, user_id pour la notif WebSocket
        """
        next_ticket_id = await get_next_ticket(org_id, agency_id, service_id)
        if not next_ticket_id:
            return {"success": False, "message": "Aucun ticket en attente"}

        ticket = await self._get_ticket_by_id(next_ticket_id, org_id)

        await self._transition(
            ticket, TicketStatus.CALLED,
            actor_id=counter_id,
            actor_role="agent",
            counter_id=counter_id,
            counter_name=counter_name,
        )
        ticket.called_at = datetime.now(timezone.utc)
        ticket.counter_id = counter_id
        ticket.counter_name = counter_name

        await remove_from_queue(org_id, agency_id, service_id, next_ticket_id)
        await self.db.commit()
        await self.db.refresh(ticket)

        logger.info(f"Ticket appelé: {ticket.number} → guichet {counter_name} | org={org_id}")

        # Notifier le client (SMS + FCM) — fire and forget
        try:
            from app.services.notification_service import NotificationService
            notif_svc = NotificationService(self.db)
            await notif_svc.notify_ticket_called(ticket, counter_name)
        except Exception as e:
            logger.error(f"Notification TICKET_CALLED échouée (non bloquant): {e}")

        return {
            "success": True,
            "ticket_id": ticket.id,
            "ticket_number": ticket.number,
            "counter_name": counter_name,
            "user_id": ticket.user_id,
        }

    # ── Marquer un ticket comme en service ────────────────────────────────────
    async def start_serving(
        self, ticket_id: str, org_id: str
    ) -> dict:
        """
        Le guichetier confirme que le client est présent et le service commence.
        Transition : CALLED → SERVING
        """
        ticket = await self._get_ticket_by_id(ticket_id, org_id)
        await self._transition(ticket, TicketStatus.SERVING)
        ticket.served_at = datetime.now(timezone.utc)
        await self.db.commit()
        return {"success": True, "ticket_id": ticket_id}

    # ── Terminer un ticket ────────────────────────────────────────────────────
    async def complete_ticket(
        self, ticket_id: str, org_id: str
    ) -> dict:
        """
        Service terminé avec succès.
        Transition : SERVING → DONE
        """
        ticket = await self._get_ticket_by_id(ticket_id, org_id)
        await self._transition(ticket, TicketStatus.DONE)
        ticket.done_at = datetime.now(timezone.utc)

        # Calculer le temps réel de service
        if ticket.served_at:
            delta = ticket.done_at - ticket.served_at
            ticket.actual_wait_minutes = int(delta.total_seconds() / 60)

        await self.db.commit()

        # Notifier le client (in-app) — fire and forget
        try:
            from app.services.notification_service import NotificationService
            notif_svc = NotificationService(self.db)
            await notif_svc.notify_ticket_done(ticket)
        except Exception as e:
            logger.error(f"Notification TICKET_DONE échouée (non bloquant): {e}")

        return {"success": True, "ticket_id": ticket_id}

    # ── Marquer un ticket comme absent ───────────────────────────────────────
    async def mark_absent(self, ticket_id: str, org_id: str) -> dict:
        """
        Le client ne s'est pas présenté après timeout (3 min).
        Transition : CALLED → ABSENT
        """
        ticket = await self._get_ticket_by_id(ticket_id, org_id)
        await self._transition(ticket, TicketStatus.ABSENT)
        await self.db.commit()
        logger.info(f"Ticket absent: {ticket.number}")
        return {"success": True, "ticket_id": ticket_id}

    # ── Remettre un ticket absent en file ─────────────────────────────────────
    async def return_to_queue(
        self, ticket_id: str, org_id: str, user_id: str | None = None
    ) -> dict:
        """
        Le client absent demande à revenir dans la file.
        Transition : ABSENT → WAITING
        Le ticket revient en fin de file (priorité dégradée).

        Args:
            ticket_id: ID du ticket absent
            org_id: Organisation de l'appelant (agent / staff)
            user_id: Si fourni (appel d'un client OTP sans organisation), le
                     ticket est retrouvé par propriétaire et l'org du ticket
                     remplace org_id. Un client ne peut donc remettre en file
                     que son propre ticket.

        Raises:
            HTTPException 400: Transition interdite (ticket non ABSENT)
            HTTPException 404: Ticket introuvable (ou appartenant à un autre user)
        """
        if user_id is not None:
            ticket = await self._get_ticket_for_owner(ticket_id, user_id)
            org_id = ticket.org_id
        else:
            ticket = await self._get_ticket_by_id(ticket_id, org_id)
        await self._transition(ticket, TicketStatus.WAITING)

        # Remettre en file avec priorité standard (pénalité) — service_id depuis le ticket
        priority_score = PRIORITY_SCORES[TicketPriority.STANDARD]
        await add_to_queue(org_id, ticket.agency_id, ticket.service_id, ticket.id, priority_score)
        await self.db.commit()
        return {"success": True, "ticket_id": ticket_id}

    # ── Historique paginé ─────────────────────────────────────────────────────
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

        # Compter le total
        count_result = await self.db.execute(
            select(func.count(Ticket.id)).where(Ticket.user_id == user_id)
        )
        total = count_result.scalar() or 0

        # Récupérer la page
        result = await self.db.execute(
            select(Ticket)
            .where(Ticket.user_id == user_id)
            .order_by(Ticket.created_at.desc())
            .limit(page_size)
            .offset(offset)
        )
        tickets = result.scalars().all()

        items = [
            TicketResponse(
                id=t.id,
                number=t.number,
                status=t.status.value,
                priority=t.priority.value,
                agency_id=t.agency_id,
                service_id=t.service_id,
                qr_token=t.qr_token,
                position=0,
                total_in_queue=0,
                estimated_wait_minutes=t.estimated_wait_minutes,
                created_at=t.created_at,
                counter_name=t.counter_name,
            )
            for t in tickets
        ]

        return TicketHistoryResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            has_next=(offset + page_size) < total,
        )

    # ── Machine à états ───────────────────────────────────────────────────────
    async def _transition(
        self,
        ticket: Ticket,
        new_status: TicketStatus,
        actor_id: str | None = None,
        actor_role: str | None = None,
        counter_id: str | None = None,
        counter_name: str | None = None,
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

        # ── Audit trail QueueLog ──────────────────────────────────────────────
        await self._log_action(
            ticket=ticket,
            action=STATUS_TO_LOG_ACTION.get(new_status, QueueLogAction.TICKET_CREATED),
            from_status=from_status,
            to_status=new_status,
            actor_id=actor_id,
            actor_role=actor_role,
            counter_id=counter_id,
            counter_name=counter_name,
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
        """
        # Calculer le temps écoulé depuis la création si non fourni
        if elapsed_seconds is None and ticket.created_at:
            now = datetime.now(timezone.utc)
            created = ticket.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            elapsed_seconds = int((now - created).total_seconds())

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
        )
        self.db.add(log)

    # ── Helpers privés ────────────────────────────────────────────────────────
    def _resolve_priority(
        self, raw_priority: str, actor_role: UserRole | None
    ) -> TicketPriority:
        """
        Valide la priorité demandée contre le rôle de l'appelant.

        Règle de sécurité : VIP et EMERGENCY ne sont JAMAIS auto-attribuables
        par un client. Sans ce contrôle, un simple `priority="emergency"` dans
        le corps de la requête suffisait à passer devant toute la file — ce qui
        détruit la promesse d'équité du produit.

        Args:
            raw_priority: Valeur brute reçue du client ("standard", "vip"…)
            actor_role: Rôle de l'appelant (None = client anonyme)

        Returns:
            La priorité validée

        Raises:
            HTTPException 400: Priorité inconnue
            HTTPException 403: Priorité réservée au staff
        """
        try:
            requested = TicketPriority(raw_priority)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "INVALID_PRIORITY",
                    "message": (
                        f"Priorité inconnue : '{raw_priority}'. "
                        f"Valeurs acceptées : {[p.value for p in TicketPriority]}"
                    ),
                },
            )

        is_staff = actor_role is not None and actor_role != UserRole.CLIENT
        if not is_staff and requested not in CLIENT_ALLOWED_PRIORITIES:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "PRIORITY_FORBIDDEN",
                    "message": (
                        f"La priorité '{requested.value}' ne peut pas être "
                        "auto-attribuée. Présentez-vous à l'accueil : un agent "
                        "l'appliquera si votre situation le justifie."
                    ),
                    "allowed_priorities": sorted(p.value for p in CLIENT_ALLOWED_PRIORITIES),
                },
            )

        return requested

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

        Args:
            ticket_id: ID du ticket
            user_id: ID du propriétaire attendu

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
        # limit(1) + first() : si deux tickets actifs existaient malgré tout
        # (création concurrente avant contrainte d'unicité), on renvoie le plus
        # ancien au lieu de lever MultipleResultsFound → 500.
        result = await self.db.execute(
            select(Ticket)
            .where(
                Ticket.user_id == user_id,
                Ticket.status.in_(ACTIVE_STATUSES),
            )
            .order_by(Ticket.created_at.asc())
            .limit(1)
        )
        return result.scalars().first()

    async def _get_next_sequence(self, agency_id: str, prefix: str) -> int:
        """
        Génère le prochain numéro de séquence pour le jour en cours.
        La séquence repart à 1 chaque jour à minuit UTC.

        Deux niveaux de protection contre les doublons :
          1. Compteur Redis atomique (INCR) — élimine la course entre requêtes
             concurrentes, qui était la cause réelle des « A-5 » en double.
          2. Repli sur MAX(sequence) + 1 si Redis est indisponible, avec
             l'index unique en base comme dernier rempart (cf. migration 007).

        MAX et non COUNT : compter les tickets du jour donnait un numéro déjà
        utilisé si un ticket avait été supprimé entre-temps.
        """
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        result = await self.db.execute(
            select(func.max(Ticket.sequence)).where(
                Ticket.agency_id == agency_id,
                Ticket.prefix == prefix,
                Ticket.created_at >= today_start,
            )
        )
        db_max = result.scalar() or 0

        try:
            return await next_daily_sequence(
                agency_id, prefix, today_start.date().isoformat(), db_max
            )
        except Exception as e:
            # Redis est un prérequis de l'app (vérifié au démarrage) : ce repli
            # ne devrait jamais s'activer. Il évite un 500 en cas de coupure.
            logger.error(
                f"Compteur Redis indisponible, repli sur MAX(sequence)+1: {e}",
                exc_info=True,
            )
            return db_max + 1
