"""
Router Counters — /api/v1/organizations/{org_id}/branches/{branch_id}/counters
Aucune logique métier ici, tout délègue à CounterService.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user, get_current_agent
from app.models.user import User, UserRole
from app.schemas.counter import (
    CreateCounterRequest,
    UpdateCounterRequest,
    CounterResponse,
    CounterListResponse,
)
from app.services.counter_service import CounterService

router = APIRouter()


def _get_effective_org_id(current_user: User, org_id: str) -> str:
    """
    Retourne l'org_id effectif selon le rôle.
    Un super_admin peut agir sur n'importe quelle org.
    Un admin_org est limité à la sienne.
    """
    from fastapi import HTTPException
    if current_user.role == UserRole.SUPER_ADMIN:
        return org_id
    if current_user.org_id != org_id:
        raise HTTPException(
            status_code=403,
            detail={"code": "FORBIDDEN", "message": "Accès refusé à cette organisation"}
        )
    return org_id


@router.post("", status_code=201)
async def create_counter(
    org_id: str,
    branch_id: str,
    body: CreateCounterRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_agent),
) -> dict:
    """Crée un nouveau guichet dans une agence. Réservé aux agents et admins."""
    effective_org_id = _get_effective_org_id(current_user, org_id)
    service = CounterService(db)
    result = await service.create(effective_org_id, branch_id, body)
    return {"success": True, "data": result, "message": "Guichet créé"}


@router.get("", response_model=None)
async def list_counters(
    org_id: str,
    branch_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    active_only: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_agent),
) -> dict:
    """Liste les guichets d'une agence."""
    effective_org_id = _get_effective_org_id(current_user, org_id)
    service = CounterService(db)
    result = await service.list(
        org_id=effective_org_id,
        agency_id=branch_id,
        page=page,
        page_size=page_size,
        active_only=active_only,
    )
    return {"success": True, "data": result}


@router.get("/{counter_id}", response_model=None)
async def get_counter(
    org_id: str,
    branch_id: str,
    counter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_agent),
) -> dict:
    """Récupère un guichet par son ID."""
    effective_org_id = _get_effective_org_id(current_user, org_id)
    service = CounterService(db)
    result = await service.get_by_id(counter_id, effective_org_id)
    return {"success": True, "data": result}


@router.patch("/{counter_id}", response_model=None)
async def update_counter(
    org_id: str,
    branch_id: str,
    counter_id: str,
    body: UpdateCounterRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_agent),
) -> dict:
    """Met à jour un guichet."""
    effective_org_id = _get_effective_org_id(current_user, org_id)
    service = CounterService(db)
    result = await service.update(counter_id, effective_org_id, body)
    return {"success": True, "data": result, "message": "Guichet mis à jour"}


@router.post("/{counter_id}/open", response_model=None)
async def open_counter(
    org_id: str,
    branch_id: str,
    counter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_agent),
) -> dict:
    """Ouvre un guichet pour recevoir des clients."""
    effective_org_id = _get_effective_org_id(current_user, org_id)
    service = CounterService(db)
    result = await service.open(counter_id, effective_org_id)
    return {"success": True, "data": result, "message": "Guichet ouvert"}


@router.post("/{counter_id}/close", response_model=None)
async def close_counter(
    org_id: str,
    branch_id: str,
    counter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_agent),
) -> dict:
    """Ferme un guichet."""
    effective_org_id = _get_effective_org_id(current_user, org_id)
    service = CounterService(db)
    result = await service.close(counter_id, effective_org_id)
    return {"success": True, "data": result, "message": "Guichet fermé"}


@router.delete("/{counter_id}", status_code=200)
async def delete_counter(
    org_id: str,
    branch_id: str,
    counter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_agent),
) -> dict:
    """Désactive un guichet (soft delete)."""
    effective_org_id = _get_effective_org_id(current_user, org_id)
    service = CounterService(db)
    await service.delete(counter_id, effective_org_id)
    return {"success": True, "message": "Guichet désactivé"}
