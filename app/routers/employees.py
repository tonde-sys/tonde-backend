"""
Router Employees — /api/v1/organizations/{org_id}/employees
Aucune logique métier ici, tout délègue à EmployeeService.

Sécurité (garde-fous au niveau du router) :
  - Anti-escalade : on ne peut attribuer que des rôles inférieurs (ou égaux pour
    ADMIN_ORG) au sien ; seul un SUPER_ADMIN crée un SUPER_ADMIN.
  - On ne peut pas modifier ni désactiver un employé de rang supérieur au sien.
  - Un ADMIN_AGENCY ne gère que les employés de SES agences.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import (
    ORG_WIDE_ROLES,
    ROLE_RANK,
    assert_can_assign_role,
    assert_org_access,
    forbidden,
    get_staff_branch_ids,
)
from app.core.database import get_db
from app.core.deps import get_current_admin_agency
from app.models.user import User, UserRole
from app.schemas.employee import CreateEmployeeRequest, UpdateEmployeeRequest
from app.services.employee_service import EmployeeService

router = APIRouter()


async def _assert_agency_scope(db: AsyncSession, actor: User, agency_id: str | None) -> None:
    """
    Un ADMIN_AGENCY ne peut gérer que des employés rattachés à une de SES agences.

    Il doit donc indiquer une agence (pas d'employé « sans agence » créé par lui)
    et cette agence doit être l'une des siennes. ADMIN_ORG / SUPER_ADMIN : aucune restriction.

    Raises:
        HTTPException 403: Agence hors du périmètre de l'admin d'agence
    """
    if actor.role in ORG_WIDE_ROLES:
        return
    if not agency_id or agency_id not in await get_staff_branch_ids(db, actor):
        raise forbidden("Vous ne pouvez gérer que les employés de votre agence", code="BRANCH_FORBIDDEN")


def _assert_can_manage_rank(actor: User, target_role: UserRole) -> None:
    """Interdit de modifier ou désactiver un employé de rang supérieur ou égal au sien (hors admin d'org)."""
    if actor.role in ORG_WIDE_ROLES:
        return
    if ROLE_RANK[target_role] >= ROLE_RANK[actor.role]:
        raise forbidden("Vous ne pouvez pas gérer un employé de rang égal ou supérieur", code="ROLE_ESCALATION")


@router.post("", status_code=201)
async def create_employee(
    org_id: str,
    body: CreateEmployeeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_agency),
) -> dict:
    """
    Crée un employé en liant un User à cette organisation.
    Réservé aux admin_agency (leur agence), admin_org et super_admin.
    """
    assert_org_access(current_user, org_id)
    assert_can_assign_role(current_user, body.role)
    await _assert_agency_scope(db, current_user, body.agency_id)
    result = await EmployeeService(db).create(org_id, body)
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
    """Liste les employés de l'organisation (un admin d'agence ne voit que ses agences)."""
    assert_org_access(current_user, org_id)
    agency_ids: set[str] | None = None
    if current_user.role not in ORG_WIDE_ROLES:
        agency_ids = await get_staff_branch_ids(db, current_user)
        if agency_id and agency_id not in agency_ids:
            raise forbidden("Cette agence n'est pas dans votre périmètre", code="BRANCH_FORBIDDEN")
    result = await EmployeeService(db).list(
        org_id=org_id,
        agency_id=agency_id,
        role=role,
        page=page,
        page_size=page_size,
        agency_ids=agency_ids,
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
    assert_org_access(current_user, org_id)
    result = await EmployeeService(db).get_by_id(employee_id, org_id)
    await _assert_agency_scope(db, current_user, result.agency_id)
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
    assert_org_access(current_user, org_id)
    service = EmployeeService(db)
    existing = await service.get_by_id(employee_id, org_id)
    await _assert_agency_scope(db, current_user, existing.agency_id)
    _assert_can_manage_rank(current_user, UserRole(existing.role))
    if body.role is not None:
        assert_can_assign_role(current_user, body.role)
    if "agency_id" in body.model_fields_set:
        await _assert_agency_scope(db, current_user, body.agency_id)
    result = await service.update(employee_id, org_id, body)
    return {"success": True, "data": result, "message": "Employé mis à jour"}


@router.delete("/{employee_id}", status_code=200)
async def deactivate_employee(
    org_id: str,
    employee_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_agency),
) -> dict:
    """Désactive un employé (soft delete). Remet le User au rôle CLIENT."""
    assert_org_access(current_user, org_id)
    service = EmployeeService(db)
    existing = await service.get_by_id(employee_id, org_id)
    await _assert_agency_scope(db, current_user, existing.agency_id)
    _assert_can_manage_rank(current_user, UserRole(existing.role))
    await service.deactivate(employee_id, org_id)
    return {"success": True, "message": "Employé désactivé"}
