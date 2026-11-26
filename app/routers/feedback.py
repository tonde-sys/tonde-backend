"""
Router Feedback — retours utilisateurs de l'application mobile.

  POST /api/v1/feedback        envoyer un avis, un signalement ou une suggestion
  GET  /api/v1/feedback/mine   historique « Mes retours »

Les deux routes exigent une session : un retour anonyme ne peut être ni suivi
ni rattaché à un passage, et le quota horaire se compte par compte, pas par IP.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.schemas.feedback import CreateFeedbackRequest
from app.services.feedback_service import FeedbackService

router = APIRouter()


@router.post("", status_code=201, summary="Envoyer un retour")
async def create_feedback(
    body: CreateFeedbackRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Enregistre un avis, un signalement de bug ou une suggestion.

    Refus possibles, tous porteurs d'un code métier pour que l'application
    affiche un message utile plutôt qu'un échec générique :

    - `FEEDBACK_RATE_LIMITED` (429) : cinq retours déjà envoyés dans l'heure
    - `FEEDBACK_ALREADY_SENT` (409) : même catégorie déjà signalée sur ce ticket
    - `TICKET_NOT_FOUND` (404) : ticket inexistant ou appartenant à quelqu'un d'autre
    - `ORGANIZATION_NOT_FOUND` (404) : organisation inexistante
    """
    data = await FeedbackService(db).create(current_user, body)
    return {"success": True, "data": data}


@router.get("/mine", summary="Mes retours")
async def list_my_feedback(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Historique paginé des retours envoyés par l'utilisateur connecté."""
    data = await FeedbackService(db).list_for_user(current_user.id, page, page_size)
    return {"success": True, "data": data}
