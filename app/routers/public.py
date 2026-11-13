"""
Router Public — endpoints sans authentification pour la salle d'attente.

  GET /api/v1/branches/{branch_id}/display   écran TV / QR d'agence

Aucune donnée personnelle n'est exposée. Limité en débit par IP.
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.middlewares import limiter
from app.services.public_service import PublicQueueService

router = APIRouter()


@router.get("/branches/{branch_id}/display", summary="Écran de salle d'attente (public)")
@limiter.limit("120/minute")
async def branch_display(
    request: Request,
    branch_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    État public d'une agence : numéro en cours à chaque guichet ouvert, et pour chaque
    service le nombre de personnes en attente et l'attente estimée.

    Sert l'écran TV de la salle (mis à jour en direct par WebSocket) et le scan du QR de
    l'agence (le client voit les services disponibles avant de prendre un ticket).
    """
    return {"success": True, "data": await PublicQueueService(db).get_branch_display(branch_id)}
