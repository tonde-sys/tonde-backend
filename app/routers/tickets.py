"""
Router Tickets — /api/v1/tickets/...

Tous les endpoints REST sont protégés par JWT (sauf le scan QR, public).
Les endpoints WebSocket utilisent l'auth par query param.
Le guichetier doit avoir le rôle AGENT minimum, et ne peut agir que sur SES tickets
(son guichet) ; superviseurs et admins agissent dans le périmètre de leur branch.

Aucune logique métier ici : tout délègue à TicketService. Les événements temps réel
sont publiés par le service, pas par le router.
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal, get_db
from app.core.deps import get_current_agent, get_current_user, get_optional_user
from app.core.middlewares import check_ws_rate_limit, limiter
from app.models.ticket import Ticket
from app.models.user import User, UserRole
from app.schemas.ticket import (
    CallNextRequest,
    CloseTicketRequest,
    CreateTicketRequest,
    SetPriorityRequest,
    TransferTicketRequest,
)
from app.services.public_service import PublicQueueService
from app.services.ticket_service import TicketService
from app.websocket.queue_ws import ws_manager

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────
def _get_org_id(current_user: User) -> str | None:
    """
    Retourne l'org_id de l'utilisateur.
    Pour les clients sans org, retourne None pour que le service
    puisse récupérer l'org depuis l'agence / le ticket.
    """
    return current_user.org_id


# ── REST endpoints : client ───────────────────────────────────────────────────

@router.post("", summary="Créer un nouveau ticket")
async def create_ticket(
    body: CreateTicketRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Le mobile appelle cet endpoint pour prendre un ticket.
    Retourne numéro, position dans la file et temps estimé.

    - Idempotent avec `client_action_id` (réseau instable : renvoyer la même requête
      ne crée jamais deux tickets).
    - Une priorité demandée (priority, vip, emergency) n'est pas appliquée
      automatiquement : le ticket démarre en standard et un agent confirme la demande.
    """
    service = TicketService(db)
    return await service.create_ticket(body, current_user.id, _get_org_id(current_user))


@router.get("/history", summary="Historique des tickets")
async def get_history(
    page: int = Query(default=1, ge=1, description="Numéro de page"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Retourne l'historique paginé des tickets de l'utilisateur connecté.
    Inclut total et has_next pour la pagination mobile.
    """
    service = TicketService(db)
    return await service.get_history(current_user.id, page)


@router.get("/qr/{qr_token}", summary="Valider un QR code de ticket")
@limiter.limit("60/minute")
async def get_ticket_by_qr(
    request: Request,
    qr_token: str,
    viewer: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Scan du QR code d'un ticket — public (pas d'authentification requise).

    Retourne uniquement des informations NON sensibles : numéro, statut, position,
    attente estimée, service et agence. Aucune donnée personnelle.
    Un ticket de plus de 24 h est introuvable (404).

    Un agent connecté de la même organisation reçoit en plus l'identifiant du ticket,
    pour enchaîner avec /serving, /done, etc.
    """
    return await PublicQueueService(db).get_ticket_by_qr(qr_token, viewer)


@router.get("/{ticket_id}", summary="Détail d'un ticket")
async def get_ticket(
    ticket_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    service = TicketService(db)
    return await service.get_ticket(
        ticket_id,
        current_user.id,
        _get_org_id(current_user),
        actor_role=current_user.role,
    )


@router.delete("/{ticket_id}", summary="Annuler un ticket")
async def cancel_ticket(
    ticket_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    service = TicketService(db)
    return await service.cancel_ticket(ticket_id, current_user.id, _get_org_id(current_user))


@router.post("/{ticket_id}/return", summary="Ticket absent revient en file")
async def return_to_queue(
    ticket_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Transition ABSENT → WAITING. Le client redemande sa place.

    Un client (sans organisation) ne peut remettre en file que son propre
    ticket : son user_id est transmis au service pour retrouver le ticket.
    Le staff (agent et plus) agit dans le périmètre de sa branch.
    """
    service = TicketService(db)
    is_client = current_user.role == UserRole.CLIENT
    return await service.return_to_queue(
        ticket_id,
        current_user.org_id or "",
        user_id=current_user.id if is_client else None,
        actor=None if is_client else current_user,
    )


# ── REST endpoints : guichetier ───────────────────────────────────────────────

@router.post("/counter/call-next", summary="Guichetier appelle le prochain ticket")
async def call_next(
    body: CallNextRequest,
    current_user: User = Depends(get_current_agent),  # ← rôle AGENT minimum
    db: AsyncSession = Depends(get_db),
):
    """
    Le guichetier appelle le prochain ticket de la file.

    Atomique (deux guichets ne peuvent pas obtenir le même ticket). L'agent doit être
    affecté au guichet, qui doit être ouvert. Notifie le client en temps réel et par
    SMS/push (multi-instance via Redis Pub/Sub).
    """
    if not current_user.org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "NO_ORG", "message": "Cet agent n'est pas associé à une organisation"},
        )
    return await TicketService(db).call_next(
        agency_id=body.agency_id,
        service_id=body.service_id,
        counter_id=body.counter_id,
        org_id=current_user.org_id,
        actor=current_user,
    )


@router.post("/{ticket_id}/serving", summary="Guichetier confirme le service")
async def start_serving(
    ticket_id: str,
    current_user: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    """Transition CALLED → SERVING. Le client est physiquement au guichet."""
    return await TicketService(db).start_serving(ticket_id, current_user)


@router.post("/{ticket_id}/done", summary="Guichetier termine le service")
async def complete_ticket(
    ticket_id: str,
    current_user: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    """Transition SERVING → DONE. Service terminé avec succès."""
    return await TicketService(db).complete_ticket(ticket_id, current_user)


@router.post("/{ticket_id}/incomplete", summary="Service interrompu")
async def mark_incomplete(
    ticket_id: str,
    body: CloseTicketRequest | None = None,
    current_user: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    """
    Transition SERVING → INCOMPLETE. Le service n'a pas pu être terminé.
    Le ticket reste actif jusqu'à /requeue (remise en file) ou /close (clôture).
    """
    return await TicketService(db).mark_incomplete(
        ticket_id, current_user, body.reason if body else None
    )


@router.post("/{ticket_id}/absent", summary="Marquer un client absent")
async def mark_absent(
    ticket_id: str,
    current_user: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    """
    Transition CALLED → ABSENT. Le client ne s'est pas présenté.
    (Le passage est aussi automatique après ABSENT_TIMEOUT_SECONDS.)
    """
    return await TicketService(db).mark_absent(ticket_id, current_user)


@router.post("/{ticket_id}/transfer", summary="Transférer un ticket vers un autre service")
async def transfer_ticket(
    ticket_id: str,
    body: TransferTicketRequest,
    current_user: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    """
    Transfère un ticket appelé ou en service vers un autre service de la même branch.

    Le client garde son ticket et son ancienneté, reçoit un nouveau numéro du service
    cible et rejoint sa file. Il est prévenu en temps réel.
    """
    return await TicketService(db).transfer_ticket(
        ticket_id, body.target_service_id, current_user, body.reason
    )


@router.post("/{ticket_id}/priority", summary="Confirmer ou attribuer une priorité")
async def decide_priority(
    ticket_id: str,
    body: SetPriorityRequest,
    current_user: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    """
    Un agent confirme (ou rejette) la priorité demandée par le client, ou en attribue une.
    Le client garde son ancienneté : seule sa priorité change.
    """
    return await TicketService(db).decide_priority(
        ticket_id, current_user, approve=body.approve, priority=body.priority
    )


@router.post("/{ticket_id}/requeue", summary="Remettre en file un ticket interrompu")
async def requeue_ticket(
    ticket_id: str,
    current_user: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    """Transition INCOMPLETE → WAITING. Le client garde sa priorité et son ancienneté."""
    return await TicketService(db).requeue_ticket(ticket_id, current_user)


@router.post("/{ticket_id}/close", summary="Clôturer un ticket")
async def close_ticket(
    ticket_id: str,
    body: CloseTicketRequest | None = None,
    current_user: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    """Clôture un ticket WAITING, ABSENT ou INCOMPLETE → CANCELLED (libère le client)."""
    return await TicketService(db).close_ticket(
        ticket_id, current_user, body.reason if body else None
    )


# ── WebSocket endpoints ───────────────────────────────────────────────────────

@router.websocket("/ws/queue/{ticket_id}")
async def websocket_queue(
    websocket: WebSocket,
    ticket_id: str,
    agency_id: str = Query(..., description="ID de l'agence"),
    token: str = Query(..., description="JWT access token"),
):
    """
    WebSocket pour le suivi temps réel d'un ticket côté client mobile.

    Le client se connecte après avoir pris un ticket. Dès la connexion il reçoit un
    SNAPSHOT de son état réel (position, ETA), puis les mises à jour automatiques.
    Le WebSocket ne rejoue pas l'historique : ce snapshot est la reprise après coupure.

    Sécurité :
    - Vérifie que le ticket appartient bien à l'utilisateur
    - Vérifie que l'agency_id correspond à celui du ticket

    Messages client → serveur : "ping" → "pong" ; "sync" → renvoie un snapshot.

    URL Flutter :
        ws://api.tonde.app/api/v1/tickets/ws/queue/$ticketId
        ?agency_id=$agencyId&token=$accessToken
    """
    from app.core.security import verify_token

    if not await check_ws_rate_limit(websocket):
        await websocket.close(code=4029, reason="Trop de connexions")
        return

    user_id = verify_token(token, token_type="access")
    if not user_id:
        await websocket.close(code=4001, reason="Token invalide")
        return

    async with AsyncSessionLocal() as session:
        ticket = (await session.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one_or_none()
        if not ticket:
            await websocket.close(code=4004, reason="Ticket introuvable")
            return
        if ticket.user_id != user_id:
            await websocket.close(code=4003, reason="Accès non autorisé à ce ticket")
            return
        if ticket.agency_id != agency_id:
            await websocket.close(code=4004, reason="Agence incohérente")
            return
        ticket_org_id = ticket.org_id or "public"

    await ws_manager.connect_client(websocket, ticket_id, agency_id)
    logger.info(f"WS connecté — ticket={ticket_id} | user={user_id} | org={ticket_org_id}")

    async def send_snapshot() -> None:
        async with AsyncSessionLocal() as session:
            snapshot = await TicketService(session).get_ws_snapshot(ticket_id, user_id)
        if snapshot:
            await websocket.send_text(json.dumps(snapshot, default=str))

    try:
        await websocket.send_text('{"type": "connected", "message": "Suivi activé"}')
        await send_snapshot()  # reprise : état réel dès la (re)connexion

        while True:
            data = (await websocket.receive_text()).strip()
            if data == "ping":
                await websocket.send_text('{"type": "pong"}')
            elif data == "sync":
                await send_snapshot()

    except WebSocketDisconnect:
        ws_manager.disconnect_client(ticket_id, agency_id)
        logger.info(f"WS déconnecté — ticket={ticket_id}")


@router.websocket("/ws/counter/{counter_id}")
async def websocket_counter(
    websocket: WebSocket,
    counter_id: str,
    token: str = Query(..., description="JWT access token"),
):
    """
    WebSocket pour le guichetier desktop.
    Reçoit les notifications en temps réel. Nécessite un token valide avec rôle AGENT minimum.

    Sécurité :
    - Vérifie que le counter_id appartient à l'organisation de l'agent
    """
    from app.core.security import verify_token
    from app.models.counter import Counter

    if not await check_ws_rate_limit(websocket):
        await websocket.close(code=4029, reason="Trop de connexions")
        return

    user_id = verify_token(token, token_type="access")
    if not user_id:
        await websocket.close(code=4001, reason="Token invalide")
        return

    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if not user or not user.is_active or user.role == UserRole.CLIENT:
            await websocket.close(code=4003, reason="Accès réservé aux agents")
            return
        if not user.org_id:
            await websocket.close(code=4003, reason="Agent sans organisation")
            return
        counter = (await session.execute(
            select(Counter).where(Counter.id == counter_id, Counter.org_id == user.org_id)
        )).scalar_one_or_none()
        if not counter:
            await websocket.close(code=4004, reason="Guichet introuvable dans votre organisation")
            return
        org_id = user.org_id

    await ws_manager.connect_counter(websocket, counter_id)
    logger.info(f"WS guichet connecté — counter={counter_id} | agent={user_id} | org={org_id}")

    try:
        await websocket.send_text('{"type": "connected", "message": "Guichet connecté"}')
        while True:
            data = (await websocket.receive_text()).strip()
            if data == "ping":
                await websocket.send_text('{"type": "pong"}')
    except WebSocketDisconnect:
        ws_manager.counter_connections.pop(counter_id, None)
        logger.info(f"WS guichet déconnecté — counter={counter_id}")
