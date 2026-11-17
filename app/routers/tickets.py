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

from app.core.access import get_staff_org_ids
from app.core.database import AsyncSessionLocal, get_db
from app.core.deps import get_current_agent, get_current_user, get_optional_user
from app.core.middlewares import check_ws_rate_limit, limiter
from app.models.counter import Counter
from app.models.employee import Employee, EmployeeStatus
from app.models.ticket import Ticket
from app.models.user import User, UserRole
from app.schemas.ticket import (
    CallNextRequest,
    CloseTicketRequest,
    CreateTicketRequest,
    QueueListResponse,
    SetPriorityRequest,
    TransferTicketRequest,
)
from app.services.public_service import PublicQueueService
from app.services.ticket_service import ANY_COUNTER_ROLES, TicketService
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


@router.get(
    "/queue",
    summary="File d'attente d'un service (staff)",
    response_model=QueueListResponse,
)
async def list_queue(
    agency_id: str = Query(..., description="Branch dont on liste la file"),
    service_id: str = Query(..., description="Service (chaque service a sa file)"),
    current_user: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    """
    Liste les tickets en attente d'un service, dans l'ordre où ils seront appelés.

    Complément indispensable de `POST /counter/call-next` : sans cette route, le
    staff ne pouvait ni voir qui attend, ni repérer un ticket fantôme (il lui
    fallait son UUID), ni anticiper la prochaine convocation.

    - Rôle minimum : AGENT. Un agent ou un superviseur ne voit que les branches
      où il est affecté ; un admin d'organisation voit toute son organisation.
    - Ordre : priorité décroissante puis ancienneté — identique à celui de
      `call-next`, donc la première ligne est le prochain ticket appelé.
    - `position` vaut 0 pour les tickets déjà appelés ou en service (hors file).
    - L'ordre est calculé depuis PostgreSQL : la route reste utilisable quand
      Redis est indisponible.
    """
    service = TicketService(db)
    return await service.list_queue(agency_id, service_id, current_user)


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


@router.get("/active", summary="Ticket actif du client")
async def get_active_ticket(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Ticket encore actif du client connecté, ou `null` s'il n'en a pas.

    Déclarée AVANT `/{ticket_id}` : FastAPI résout les routes dans l'ordre de
    déclaration, et une route littérale placée après le paramètre serait lue
    comme un identifiant — `GET /tickets/active` chercherait le ticket nommé
    « active » et répondrait 404.

    Renvoie une enveloppe (`success`/`data`) plutôt que l'objet nu comme ses
    voisines : « aucun ticket » est un état NORMAL, pas une erreur, et un 404
    serait interprété par le mobile comme une panne réseau. Le client doit
    pouvoir distinguer les trois cas — ticket actif, aucun ticket, serveur
    injoignable — avec un seul appel.
    """
    ticket = await TicketService(db).get_active_ticket(current_user.id)
    return {
        "success": True,
        "data": ticket.model_dump(mode="json") if ticket is not None else None,
    }


@router.get("/{ticket_id}", summary="Détail d'un ticket")
async def get_ticket(
    ticket_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    service = TicketService(db)
    return await service.get_ticket(ticket_id, current_user)


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


@router.post("/{ticket_id}/recall", summary="Relancer l'annonce d'un ticket appelé")
async def recall_ticket(
    ticket_id: str,
    current_user: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    """
    Relance l'annonce d'un client déjà appelé qui ne s'est pas présenté.

    Rediffuse l'appel (salle + téléphone), renvoie le SMS et **remet le compte à
    rebours ABSENT à zéro**. Plafonné par `MAX_RECALLS_PER_TICKET` : chaque
    relance coûte un SMS.

    Sans cet endpoint, la seule action possible était de déclarer le client
    ABSENT — c'est-à-dire de le renvoyer derrière tout le monde parce qu'il
    n'avait pas entendu son numéro.
    """
    return await TicketService(db).recall(ticket_id, current_user)


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
        logger.info(f"WS déconnecté — ticket={ticket_id}")
    finally:
        # Constat P3-9 : on ne retire QUE cette connexion. Avant, la
        # déconnexion d'un appareil supprimait l'entrée du ticket entier, donc
        # le suivi du second appareil — silencieusement.
        ws_manager.disconnect_client(ticket_id, agency_id, websocket)


@router.websocket("/ws/counter/{counter_id}")
async def websocket_counter(
    websocket: WebSocket,
    counter_id: str,
    token: str = Query(..., description="JWT access token"),
):
    """
    WebSocket pour le guichetier desktop.
    Reçoit les notifications en temps réel. Nécessite un token valide avec rôle AGENT minimum.

    Sécurité (constat P3-10 de l'audit) :
    - le guichet doit appartenir à une organisation où l'agent est rattaché ;
    - et l'agent doit y être **affecté** : un agent du guichet 1 ne peut pas
      s'abonner au flux du guichet 7.

    Le contrôle d'affectation existait déjà pour le REST
    (`TicketService._verify_agent_assignment`) mais pas ici : n'importe quel
    agent de l'organisation pouvait écouter les événements d'un autre guichet,
    donc voir les tickets appelés et servis par un collègue.

    L'autorisation elle-même est dans `_authorize_counter_stream`, testable sans
    ouvrir de socket.
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
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        counter, refusal = await _authorize_counter_stream(session, user, counter_id)

    if refusal is not None:
        code, reason = refusal
        await websocket.close(code=code, reason=reason)
        logger.warning(f"WS guichet refusé — agent={user_id} counter={counter_id} : {reason}")
        return

    await ws_manager.connect_counter(websocket, counter_id, agency_id=counter.agency_id)
    logger.info(f"WS guichet connecté — counter={counter_id} | agent={user_id} | org={counter.org_id}")

    try:
        await websocket.send_text('{"type": "connected", "message": "Guichet connecté"}')
        while True:
            data = (await websocket.receive_text()).strip()
            if data == "ping":
                await websocket.send_text('{"type": "pong"}')
    except WebSocketDisconnect:
        logger.info(f"WS guichet déconnecté — counter={counter_id}")
    finally:
        # Un guichet peut avoir deux écrans : on ne retire que celui qui part.
        ws_manager.disconnect_counter(counter_id, websocket)


async def _authorize_counter_stream(
    session: AsyncSession, user: User | None, counter_id: str
) -> tuple[Counter | None, tuple[int, str] | None]:
    """
    Autorise — ou refuse — l'abonnement au flux d'un guichet.

    Isolée du handler WebSocket pour être testable sans ouvrir de socket : c'est
    la seule partie qui contient une décision, le reste est du transport.

    Règles, alignées sur le REST (`_verify_agent_assignment`) :
      - CLIENT : refusé (le flux d'un guichet n'est pas public) ;
      - aucun rattachement staff actif : refusé (compte mal configuré) ;
      - guichet d'une organisation étrangère : refusé ;
      - AGENT non affecté à ce guichet : refusé ;
      - SUPERVISOR et au-dessus : accepté dans leur périmètre d'organisation.

    Le rattachement est lu sur `Employee` (actif) ET sur `User.org_id` : ce
    dernier seul ne suffit pas — il vaut NULL pour un `super_admin`, et il peut
    désigner une organisation dont le rattachement a été retiré alors qu'un autre
    subsiste (voir `app.core.access.get_staff_org_ids`).

    Returns:
        (counter, None) si autorisé ; (None, (code_fermeture, raison)) sinon.
    """
    if user is None or not user.is_active or user.role == UserRole.CLIENT:
        return None, (4003, "Accès réservé aux agents")

    org_ids = await get_staff_org_ids(session, user)
    if user.role != UserRole.SUPER_ADMIN and not org_ids:
        return None, (4003, "Compte staff sans rattachement à une organisation")

    counter = (await session.execute(
        select(Counter).where(Counter.id == counter_id)
    )).scalar_one_or_none()
    if counter is None or (
        user.role != UserRole.SUPER_ADMIN and counter.org_id not in org_ids
    ):
        return None, (4004, "Guichet introuvable dans votre organisation")

    if user.role not in ANY_COUNTER_ROLES:
        assigned = (await session.execute(
            select(Employee.id).where(
                Employee.user_id == user.id,
                Employee.org_id == counter.org_id,
                Employee.counter_id == counter.id,
                Employee.status == EmployeeStatus.ACTIVE,
            )
        )).scalar_one_or_none()
        if assigned is None:
            return None, (4003, "Vous n'êtes pas affecté à ce guichet")

    return counter, None
