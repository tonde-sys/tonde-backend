"""
Router Notifications — boîte de réception in-app de l'utilisateur connecté.

  GET  /api/v1/notifications                liste paginée (les plus récentes d'abord)
  POST /api/v1/notifications/read-all       tout marquer comme lu
  POST /api/v1/notifications/{id}/read      marquer une notification comme lue

Les notifications in-app (ticket terminé, absence…) étaient créées mais jamais
consultables ni marquables comme lues.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.services.notification_service import NotificationService

router = APIRouter()


@router.get("", summary="Mes notifications")
async def list_notifications(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    unread_only: bool = Query(False, description="Seulement les non lues"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Liste les notifications in-app, avec le nombre de non-lues (pour le badge de l'application)."""
    data = await NotificationService(db).list_for_user(current_user.id, page, page_size, unread_only)
    return {"success": True, "data": data}


@router.post("/read-all", summary="Tout marquer comme lu")
async def mark_all_read(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Marque toutes les notifications non lues comme lues. Retourne leur nombre."""
    updated = await NotificationService(db).mark_all_read(current_user.id)
    return {"success": True, "data": {"updated": updated}}


@router.post("/{notification_id}/read", summary="Marquer comme lue")
async def mark_read(
    notification_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Marque une notification comme lue.
    404 si elle n'existe pas ou n'appartient pas à l'utilisateur (aucune fuite d'information).
    """
    if not await NotificationService(db).mark_read(current_user.id, notification_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOTIFICATION_NOT_FOUND", "message": "Notification introuvable"},
        )
    return {"success": True}
