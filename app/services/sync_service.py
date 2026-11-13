"""
SyncService — synchronisation bidirectionnelle pour le mode hors-ligne.

TONDE vise des réseaux instables : le mobile doit pouvoir agir hors-ligne puis se
resynchroniser sans jamais créer de doublon ni perdre d'état.

  GET  /sync/state     état complet (ticket actif, récents, non-lus) — premier démarrage
  POST /sync/actions   rejoue les actions faites hors-ligne, avec résultat par action
  GET  /sync/changes   ce qui a changé depuis `since` (fenêtre max SYNC_WINDOW_DAYS)

Idempotence (une action synchronisée 2 fois n'a pas d'effet double) :
  - create_ticket     : clé (user, client_action_id)
  - cancel_ticket     : déjà annulé → « already_applied »
  - return_to_queue   : déjà en file → « already_applied »

Résolution de conflit : le SERVEUR fait foi. Un conflit (ex. annuler un ticket déjà
appelé) n'écrase rien : la réponse contient l'état réel du ticket pour que le mobile
s'aligne. Les numéros ne peuvent pas entrer en conflit : seul le serveur les attribue.
"""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.notification import Notification, NotificationChannel
from app.models.queue_log import QueueLog
from app.models.ticket import Ticket, TicketStatus
from app.models.user import User, UserRole
from app.schemas.sync import SyncAction, SyncActionResult
from app.schemas.ticket import CreateTicketRequest
from app.services.ticket_service import TicketService, as_utc

logger = logging.getLogger(__name__)

MAX_CHANGES_PER_CALL = 500


class SyncService:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.tickets = TicketService(db)

    # ── État complet ──────────────────────────────────────────────────────────

    async def get_state(self, user: User) -> dict:
        """
        État complet du client : ticket actif, tickets récents et notifications non lues.

        À appeler au premier démarrage ou après une très longue absence
        (au-delà de la fenêtre de /sync/changes).

        Returns:
            {active_ticket, recent_tickets, unread_notifications, server_time}
        """
        active = await self.tickets._get_active_ticket(user.id)
        since = datetime.now(timezone.utc) - timedelta(days=settings.SYNC_WINDOW_DAYS)
        recent = (await self.db.execute(
            select(Ticket)
            .where(Ticket.user_id == user.id, Ticket.created_at >= since)
            .order_by(Ticket.created_at.desc())
            .limit(50)
        )).scalars().all()
        unread = (await self.db.execute(
            select(func.count(Notification.id)).where(
                Notification.user_id == user.id,
                Notification.channel == NotificationChannel.IN_APP,
                Notification.is_read == False,  # noqa: E712
            )
        )).scalar() or 0

        return {
            "active_ticket": await self._ticket_state(active) if active else None,
            "recent_tickets": [await self._ticket_state(t) for t in recent],
            "unread_notifications": unread,
            "server_time": datetime.now(timezone.utc),
        }

    # ── Actions hors-ligne ────────────────────────────────────────────────────

    async def apply_actions(self, user: User, actions: list[SyncAction]) -> list[SyncActionResult]:
        """
        Rejoue, dans l'ordre, les actions faites hors-ligne.

        Chaque action est traitée indépendamment : une erreur n'empêche pas les
        suivantes, et le résultat dit précisément ce qui s'est passé pour chacune.

        Args:
            user: Utilisateur connecté (les actions ne concernent que SES tickets)
            actions: Actions dans l'ordre où l'utilisateur les a faites

        Returns:
            Un SyncActionResult par action, dans le même ordre.
        """
        results: list[SyncActionResult] = []
        oldest_allowed = datetime.now(timezone.utc) - timedelta(days=settings.SYNC_WINDOW_DAYS)

        for action in actions:
            if action.client_created_at and as_utc(action.client_created_at) < oldest_allowed:
                results.append(self._result(
                    action, "rejected", code="ACTION_TOO_OLD",
                    message=f"Action plus ancienne que {settings.SYNC_WINDOW_DAYS} jours : ignorée",
                ))
                continue
            try:
                results.append(await self._apply_one(user, action))
            except HTTPException as exc:
                await self._recover(user)
                results.append(await self._from_http_error(user, action, exc))
            except (ValidationError, KeyError, ValueError) as exc:
                await self._recover(user)
                results.append(self._result(action, "rejected", code="INVALID_PAYLOAD", message=str(exc)[:200]))
            except Exception as exc:  # noqa: BLE001 — une action en erreur ne bloque pas les autres
                await self._recover(user)
                logger.error(f"Sync : action {action.client_action_id} en erreur : {exc}", exc_info=True)
                results.append(self._result(action, "rejected", code="SERVER_ERROR", message="Erreur serveur"))
        return results

    async def _recover(self, user: User) -> None:
        """
        Annule la transaction en cours après une action en erreur, puis recharge l'utilisateur.

        Un rollback expire TOUS les objets de la session : sans ce rechargement, l'action
        suivante lirait `user.id` sur un objet expiré (MissingGreenlet en async).
        """
        await self.db.rollback()
        await self.db.refresh(user)

    async def _apply_one(self, user: User, action: SyncAction) -> SyncActionResult:
        """Applique UNE action. Les HTTPException du Queue Engine sont traitées par l'appelant."""
        if action.type == "create_ticket":
            already = await self.tickets._find_by_client_action(user.id, action.client_action_id)
            ticket = await self.tickets.create_ticket(
                CreateTicketRequest(
                    agency_id=action.payload["agency_id"],
                    service_id=action.payload["service_id"],
                    priority=action.payload.get("priority", "standard"),
                    client_action_id=action.client_action_id,
                ),
                user.id,
                user.org_id,
            )
            return self._result(
                action, "already_applied" if already else "applied", ticket=ticket.model_dump(mode="json")
            )

        ticket_row = await self._own_ticket(user, action.payload["ticket_id"])

        if action.type == "cancel_ticket":
            if ticket_row.status == TicketStatus.CANCELLED:
                return self._result(action, "already_applied", ticket=await self._ticket_state(ticket_row))
            await self.tickets.cancel_ticket(ticket_row.id, user.id, user.org_id)
            return self._result(action, "applied", ticket=await self._ticket_state(ticket_row))

        # return_to_queue
        if ticket_row.status == TicketStatus.WAITING:
            return self._result(action, "already_applied", ticket=await self._ticket_state(ticket_row))
        await self.tickets.return_to_queue(
            ticket_row.id, user.org_id or "",
            user_id=user.id if user.role == UserRole.CLIENT else None,
            actor=None if user.role == UserRole.CLIENT else user,
        )
        return self._result(action, "applied", ticket=await self._ticket_state(ticket_row))

    async def _from_http_error(self, user: User, action: SyncAction, exc: HTTPException) -> SyncActionResult:
        """
        Traduit une erreur du Queue Engine en résultat de synchronisation.

        - CONFLIT : l'état du serveur contredit l'action (ticket déjà actif, ticket déjà
          appelé…) → on renvoie l'état réel du ticket pour que le mobile s'aligne.
        - REFUSÉ : l'action est invalide en elle-même (agence fermée, service inconnu…).
        """
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "ERROR", "message": str(exc.detail)}
        code = detail.get("code", "ERROR")

        ticket_state = None
        if code == "TICKET_ALREADY_ACTIVE" and detail.get("active_ticket_id"):
            active = await self.tickets._get_active_ticket(user.id)
            ticket_state = await self._ticket_state(active) if active else None
        elif action.payload.get("ticket_id"):
            try:
                ticket_state = await self._ticket_state(await self._own_ticket(user, action.payload["ticket_id"]))
            except HTTPException:
                ticket_state = None

        conflict_codes = {"TICKET_ALREADY_ACTIVE", "INVALID_TRANSITION", "TICKET_CREATION_IN_PROGRESS"}
        return self._result(
            action, "conflict" if code in conflict_codes else "rejected",
            code=code, message=detail.get("message"), ticket=ticket_state,
        )

    # ── Changements depuis une date ───────────────────────────────────────────

    async def get_changes(self, user: User, since: datetime) -> dict:
        """
        Tout ce qui a changé pour l'utilisateur depuis `since`.

        Le client met à jour son état local avec `tickets` (états actuels des tickets
        modifiés) et peut rejouer `changes` (transitions dans l'ordre chronologique).

        Args:
            user: Utilisateur connecté
            since: Dernière synchronisation du client (ISO 8601, avec fuseau)

        Returns:
            {changes, tickets, notifications, has_more, server_time}

        Raises:
            HTTPException 400: `since` plus ancien que la fenêtre de synchronisation
        """
        since = as_utc(since)
        now = datetime.now(timezone.utc)
        if since < now - timedelta(days=settings.SYNC_WINDOW_DAYS):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "SINCE_TOO_OLD",
                    "message": (
                        f"Synchronisation limitée à {settings.SYNC_WINDOW_DAYS} jours. "
                        "Rechargez l'état complet avec GET /sync/state."
                    ),
                },
            )

        logs = (await self.db.execute(
            select(QueueLog)
            .join(Ticket, Ticket.id == QueueLog.ticket_id)
            .where(Ticket.user_id == user.id, QueueLog.created_at > since)
            .order_by(QueueLog.created_at.asc())
            .limit(MAX_CHANGES_PER_CALL + 1)
        )).scalars().all()
        has_more = len(logs) > MAX_CHANGES_PER_CALL
        logs = logs[:MAX_CHANGES_PER_CALL]

        ticket_ids = list(dict.fromkeys(log.ticket_id for log in logs))
        tickets = []
        if ticket_ids:
            rows = (await self.db.execute(select(Ticket).where(Ticket.id.in_(ticket_ids)))).scalars().all()
            tickets = [await self._ticket_state(t) for t in rows]

        notifications = (await self.db.execute(
            select(Notification).where(
                Notification.user_id == user.id,
                Notification.channel == NotificationChannel.IN_APP,
                Notification.created_at > since,
            ).order_by(Notification.created_at.asc()).limit(100)
        )).scalars().all()

        return {
            "changes": [
                {
                    "ticket_id": log.ticket_id,
                    "action": log.action.value,
                    "from_status": log.from_status,
                    "to_status": log.to_status,
                    "at": log.created_at,
                }
                for log in logs
            ],
            "tickets": tickets,
            "notifications": [
                {"id": n.id, "ticket_id": n.ticket_id, "title": n.title, "body": n.body,
                 "is_read": n.is_read, "created_at": n.created_at}
                for n in notifications
            ],
            "has_more": has_more,
            "server_time": now,
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _own_ticket(self, user: User, ticket_id: str) -> Ticket:
        """Ticket appartenant à l'utilisateur (404 sinon — on ne révèle pas ceux des autres)."""
        return await self.tickets._get_ticket_for_owner(ticket_id, user.id)

    async def _ticket_state(self, ticket: Ticket | None) -> dict | None:
        """État actuel d'un ticket (position, ETA…) prêt à sérialiser en JSON."""
        if ticket is None:
            return None
        await self.db.refresh(ticket)
        return (await self.tickets._build_response(ticket)).model_dump(mode="json")

    @staticmethod
    def _result(
        action: SyncAction,
        status_: str,
        *,
        code: str | None = None,
        message: str | None = None,
        ticket: dict | None = None,
    ) -> SyncActionResult:
        return SyncActionResult(
            client_action_id=action.client_action_id,
            type=action.type,
            status=status_,
            code=code,
            message=message,
            ticket=ticket,
        )
