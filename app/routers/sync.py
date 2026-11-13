"""
Router Sync — /api/v1/sync/...  (mode hors-ligne)

  GET  /sync/state     état complet du client
  POST /sync/actions   rejoue les actions faites hors-ligne (idempotent)
  GET  /sync/changes   changements depuis une date (fenêtre de 7 jours)

Aucune logique ici : tout délègue à SyncService.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.middlewares import limiter
from app.models.user import User
from app.schemas.sync import SyncActionsRequest, SyncActionsResponse
from app.services.sync_service import SyncService

router = APIRouter()


@router.get("/state", summary="État complet du client")
async def sync_state(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Ticket actif, tickets récents (7 jours) et notifications non lues."""
    return {"success": True, "data": await SyncService(db).get_state(current_user)}


@router.post("/actions", summary="Rejouer les actions faites hors-ligne", response_model=SyncActionsResponse)
@limiter.limit("30/minute")
async def sync_actions(
    request: Request,
    body: SyncActionsRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SyncActionsResponse:
    """
    Envoie les actions (création / annulation / retour en file) faites sans réseau.

    Idempotent : une action rejouée (même client_action_id) renvoie « already_applied »
    sans effet. Chaque résultat contient l'état serveur du ticket : le mobile remplace
    son état local par celui-ci (le serveur fait foi).
    """
    results = await SyncService(db).apply_actions(current_user, body.actions)
    return SyncActionsResponse(results=results, server_time=datetime.now().astimezone())


@router.get("/changes", summary="Changements depuis une date")
async def sync_changes(
    since: datetime = Query(..., description="Dernière synchronisation (ISO 8601, ex. 2026-01-01T10:00:00Z)"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Transitions de tickets, états actuels et notifications depuis `since`.
    Au-delà de 7 jours : erreur SINCE_TOO_OLD, rechargez /sync/state.
    """
    return {"success": True, "data": await SyncService(db).get_changes(current_user, since)}
