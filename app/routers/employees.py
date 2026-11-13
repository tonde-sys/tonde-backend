"""
Router Employees — /api/v1/organizations/{org_id}/employees
Aucune logique métier ici, tout délègue à EmployeeService.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user, get_current_admin_agency
from app.models.user import User, UserRole
from app.schemas.employee import (
    CreateEmployeeRequest,
    UpdateEmployeeRequest,
    EmployeeResponse,
    EmployeeListResponse,
)
from app.services.employee_service import EmployeeService

router = APIRouter()


def _get_effective_org_id(current_user: User, org_id: str) -> str:
    """
    Retourne l'org_id effectif selon le rôle.
    Un super_admin peut agir sur n'importe quelle org.
    Un admin est limité à la sienne.
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
async def create_employee(
    org_id: str,
    body: CreateEmployeeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_agency),
) -> dict:
    """
    Crée un employé en liant un User à cette organisation.
    Réservé aux admin_agency, admin_org et super_admin.
    """
    effective_org_id = _get_effective_org_id(current_user, org_id)
    service = EmployeeService(db)
    result = await service.create(effective_org_id, body, caller=current_user)
    return {"success": True, "data": result, "message": "Employé créé"}


@router.get("", response_model=None)
async def list_employees(
    org_id: str,
    agency_id: str | None = Query(None, description="Filtrer par agence"),
    role: UserRole | None = Query(None, description="Filtrer par rôle"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_agency),
) -> dict:
    """Liste les employés de l'organisation avec filtres optionnels."""
    effective_org_id = _get_effective_org_id(current_user, org_id)
    service = EmployeeService(db)
    result = await service.list(
        org_id=effective_org_id,
        agency_id=agency_id,
        role=role,
        page=page,
        page_size=page_size,
    )
    return {"success": True, "data": result}


@router.get("/{employee_id}", response_model=None)
async def get_employee(
    org_id: str,
    employee_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_agency),
) -> dict:
    """Récupère un employé par son ID."""
    effective_org_id = _get_effective_org_id(current_user, org_id)
    service = EmployeeService(db)
    result = await service.get_by_id(employee_id, effective_org_id)
    return {"success": True, "data": result}


@router.patch("/{employee_id}", response_model=None)
async def update_employee(
    org_id: str,
    employee_id: str,
    body: UpdateEmployeeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_agency),
) -> dict:
    """Met à jour le rôle, l'affectation ou le statut d'un employé."""
    effective_org_id = _get_effective_org_id(current_user, org_id)
    service = EmployeeService(db)
    result = await service.update(employee_id, effective_org_id, body, caller=current_user)
    return {"success": True, "data": result, "message": "Employé mis à jour"}


@router.delete("/{employee_id}", status_code=200)
async def deactivate_employee(
    org_id: str,
    employee_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_agency),
) -> dict:
    """Désactive un employé (soft delete). Remet le User au rôle CLIENT."""
    effective_org_id = _get_effective_org_id(current_user, org_id)
    service = EmployeeService(db)
    await service.deactivate(employee_id, effective_org_id)
    return {"success": True, "message": "Employé désactivé"}
