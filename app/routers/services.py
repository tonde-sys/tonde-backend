"""
Router Services — CRUD des services d'une branch.

Endpoints :
  POST   /api/v1/organizations/{org_id}/branches/{branch_id}/services
  GET    /api/v1/organizations/{org_id}/branches/{branch_id}/services
  GET    /api/v1/branches/{branch_id}/services  ← public pour le mobile
  GET    /api/v1/organizations/{org_id}/branches/{branch_id}/services/{service_id}
  PATCH  /api/v1/organizations/{org_id}/branches/{branch_id}/services/{service_id}
  DELETE /api/v1/organizations/{org_id}/branches/{branch_id}/services/{service_id}

Aucune logique métier ici, tout délègue à ServiceService.
Écriture : ADMIN_AGENCY (sa branch uniquement), ADMIN_ORG, SUPER_ADMIN.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import assert_branch_access
from app.core.database import get_db
from app.core.deps import get_current_admin_agency
from app.models.user import User
from app.schemas.service import CreateServiceRequest, UpdateServiceRequest
from app.services.service_service import ServiceService

router = APIRouter()


# ── Endpoints protégés (admin) ────────────────────────────────────────────────

@router.post(
    "/organizations/{org_id}/branches/{branch_id}/services",
    status_code=201,
    summary="Créer un service dans une branch",
)
async def create_service(
    org_id: str,
    branch_id: str,
    body: CreateServiceRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_agency),
) -> dict:
    """Crée un service dans une branch. Réservé aux admins de cette branch."""
    await assert_branch_access(db, current_user, org_id, branch_id)
    result = await ServiceService(db).create(org_id=org_id, branch_id=branch_id, data=body)
    return {"success": True, "data": result, "message": "Service créé"}


@router.get(
    "/organizations/{org_id}/branches/{branch_id}/services",
    summary="Lister les services d'une branch (admin)",
)
async def list_services_admin(
    org_id: str,
    branch_id: str,
    active_only: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_agency),
) -> dict:
    """Liste les services d'une branch (y compris inactifs). Réservé aux admins."""
    await assert_branch_access(db, current_user, org_id, branch_id)
    items = await ServiceService(db).list(branch_id=branch_id, org_id=org_id, active_only=active_only)
    return {"success": True, "data": items}


@router.get(
    "/organizations/{org_id}/branches/{branch_id}/services/{service_id}",
    summary="Détail d'un service",
)
async def get_service(
    org_id: str,
    branch_id: str,
    service_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_agency),
) -> dict:
    await assert_branch_access(db, current_user, org_id, branch_id)
    result = await ServiceService(db).get_by_id(service_id=service_id, branch_id=branch_id, org_id=org_id)
    return {"success": True, "data": result}


@router.patch(
    "/organizations/{org_id}/branches/{branch_id}/services/{service_id}",
    summary="Mettre à jour un service",
)
async def update_service(
    org_id: str,
    branch_id: str,
    service_id: str,
    body: UpdateServiceRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_agency),
) -> dict:
    await assert_branch_access(db, current_user, org_id, branch_id)
    result = await ServiceService(db).update(
        service_id=service_id, branch_id=branch_id, org_id=org_id, data=body
    )
    return {"success": True, "data": result, "message": "Service mis à jour"}


@router.delete(
    "/organizations/{org_id}/branches/{branch_id}/services/{service_id}",
    summary="Désactiver un service",
)
async def delete_service(
    org_id: str,
    branch_id: str,
    service_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_agency),
) -> dict:
    await assert_branch_access(db, current_user, org_id, branch_id)
    await ServiceService(db).delete(service_id=service_id, branch_id=branch_id, org_id=org_id)
    return {"success": True, "message": "Service désactivé"}


# ── Endpoint public (mobile) ──────────────────────────────────────────────────

@router.get(
    "/branches/{branch_id}/services",
    summary="Lister les services d'une branch (public — mobile)",
)
async def list_services_public(
    branch_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Liste les services actifs d'une branch.
    Accessible sans authentification — utilisé par le mobile
    pour afficher les services disponibles avant de prendre un ticket.
    """
    items = await ServiceService(db).list(branch_id=branch_id, org_id=None, active_only=True)
    return {"success": True, "data": items}
