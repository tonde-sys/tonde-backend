"""
Router Counters — /api/v1/organizations/{org_id}/branches/{branch_id}/counters
Aucune logique métier ici, tout délègue à CounterService.

Permissions :
  - Créer / modifier / désactiver un guichet : ADMIN_AGENCY (sa branch), ADMIN_ORG, SUPER_ADMIN
  - Lister / consulter : tout membre du staff de la branch
  - Ouvrir / fermer : l'agent affecté à CE guichet, ou un superviseur et plus de la branch
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import assert_branch_access, forbidden
from app.core.database import get_db
from app.core.deps import get_current_admin_agency, get_current_agent
from app.models.employee import Employee, EmployeeStatus
from app.models.user import User, UserRole
from app.schemas.counter import CreateCounterRequest, UpdateCounterRequest
from app.services.counter_service import CounterService

router = APIRouter()


async def _assert_can_operate_counter(db: AsyncSession, user: User, counter_id: str) -> None:
    """
    Un AGENT ne peut ouvrir/fermer que SON guichet ; superviseur et plus : tout guichet de leur branch.

    Raises:
        HTTPException 403: Agent non affecté à ce guichet
    """
    if user.role != UserRole.AGENT:
        return
    assigned = (await db.execute(
        select(Employee.id).where(
            Employee.user_id == user.id,
            Employee.counter_id == counter_id,
            Employee.status == EmployeeStatus.ACTIVE,
        )
    )).scalar_one_or_none()
    if assigned is None:
        raise forbidden("Vous n'êtes pas affecté à ce guichet", code="COUNTER_NOT_ASSIGNED")


@router.post("", status_code=201)
async def create_counter(
    org_id: str,
    branch_id: str,
    body: CreateCounterRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_agency),
) -> dict:
    """Crée un nouveau guichet dans une agence. Réservé aux administrateurs de l'agence."""
    await assert_branch_access(db, current_user, org_id, branch_id)
    result = await CounterService(db).create(org_id, branch_id, body)
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
    await assert_branch_access(db, current_user, org_id, branch_id)
    result = await CounterService(db).list(
        org_id=org_id,
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
    await assert_branch_access(db, current_user, org_id, branch_id)
    result = await CounterService(db).get_by_id(counter_id, org_id, agency_id=branch_id)
    return {"success": True, "data": result}


@router.patch("/{counter_id}", response_model=None)
async def update_counter(
    org_id: str,
    branch_id: str,
    counter_id: str,
    body: UpdateCounterRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_agency),
) -> dict:
    """Met à jour un guichet. Réservé aux administrateurs de l'agence."""
    await assert_branch_access(db, current_user, org_id, branch_id)
    result = await CounterService(db).update(counter_id, org_id, body, agency_id=branch_id)
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
    await assert_branch_access(db, current_user, org_id, branch_id)
    await _assert_can_operate_counter(db, current_user, counter_id)
    result = await CounterService(db).open(counter_id, org_id, agency_id=branch_id)
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
    await assert_branch_access(db, current_user, org_id, branch_id)
    await _assert_can_operate_counter(db, current_user, counter_id)
    result = await CounterService(db).close(counter_id, org_id, agency_id=branch_id)
    return {"success": True, "data": result, "message": "Guichet fermé"}


@router.delete("/{counter_id}", status_code=200)
async def delete_counter(
    org_id: str,
    branch_id: str,
    counter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_agency),
) -> dict:
    """Désactive un guichet (soft delete). Réservé aux administrateurs de l'agence."""
    await assert_branch_access(db, current_user, org_id, branch_id)
    await CounterService(db).delete(counter_id, org_id, agency_id=branch_id)
    return {"success": True, "message": "Guichet désactivé"}
