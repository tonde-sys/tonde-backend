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


@router.get("/public/organizations", summary="Organisations actives (public — mobile)")
@limiter.limit("60/minute")
async def list_public_organizations(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Organisations actives, sans authentification.

    Sous `/public/` et non à la racine : `/organizations` est déjà pris par le
    router d'administration, qui répond 403 à un client. Deux routes de même
    chemin ne peuvent pas coexister, et la première déclarée gagne — la version
    publique serait silencieusement inatteignable.

    Aucune donnée personnelle : ni e-mail, ni téléphone, ni code d'invitation.
    """
    return {"success": True, "data": await PublicQueueService(db).list_organizations()}


@router.get(
    "/public/organizations/{org_id}/branches",
    summary="Agences actives d'une organisation (public — mobile)",
)
@limiter.limit("60/minute")
async def list_public_branches(
    request: Request,
    org_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Agences actives d'une organisation, avec le nombre de personnes en attente.

    Même raison d'être que la route ci-dessus : `/{org_id}/branches` exige un
    rôle administrateur, donc l'écran « choisir une agence » du mobile restait
    vide pour un client.

    `waiting_count` est un agrégat lu en base — aucune donnée de ticket, aucun
    numéro, aucune position ne sort ici.
    """
    return {"success": True, "data": await PublicQueueService(db).list_branches(org_id)}


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
