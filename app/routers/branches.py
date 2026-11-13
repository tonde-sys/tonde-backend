"""
Router Branches — /api/v1/organizations/{org_id}/branches

Sprint 2 — S2-01 : renommage Agency → Branch.

Routes rétro-compatibles /agencies maintenues via alias pour ne pas
casser le mobile avant coordination (voir DÉCISION 8).
Aucune logique métier ici, tout délègue à BranchService.
"""
from fastapi import APIRouter, Depends, Query, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import assert_branch_access
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.user import User, UserRole
from app.schemas.branch import (
    CreateBranchRequest,
    UpdateBranchRequest,
)
from app.schemas.branch_config import UpdateBranchConfigRequest
from app.services.branch_service import BranchService

router = APIRouter()


# ── Guards ────────────────────────────────────────────────────────────────────

def _require_branch_admin(current_user: User) -> User:
    """Vérifie que l'utilisateur a un rôle d'administration de branch ou supérieur."""
    allowed = (
        UserRole.ADMIN_AGENCY,
        UserRole.ADMIN_ORG,
        UserRole.SUPER_ADMIN,
    )
    if current_user.role not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": "Accès réservé aux administrateurs"},
        )
    return current_user


def _require_org_admin(current_user: User) -> User:
    """Créer ou supprimer une branch engage toute l'organisation : ADMIN_ORG ou SUPER_ADMIN requis."""
    if current_user.role not in (UserRole.ADMIN_ORG, UserRole.SUPER_ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": "Seul un administrateur d'organisation peut créer ou supprimer une agence",
            },
        )
    return current_user


def _check_org_access(current_user: User, target_org_id: str) -> None:
    """
    Vérifie que l'utilisateur peut agir sur l'organisation cible.

    Règles :
    - SUPER_ADMIN : peut agir sur toutes les orgs
    - ADMIN_ORG   : peut agir sur sa propre org uniquement
    - ADMIN_AGENCY: ne peut pas créer de branches dans d'autres orgs
    """
    if current_user.role == UserRole.SUPER_ADMIN:
        return

    if current_user.role in (UserRole.ADMIN_ORG, UserRole.ADMIN_AGENCY):
        if current_user.org_id != target_org_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "FORBIDDEN",
                    "message": "Vous ne pouvez agir que sur votre propre organisation",
                },
            )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/{org_id}/branches", status_code=201, summary="Créer une branch")
async def create_branch(
    org_id: str,
    body: CreateBranchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    _require_org_admin(current_user)
    _check_org_access(current_user, org_id)
    svc = BranchService(db)
    result = await svc.create(org_id, body, current_user.org_id)
    return {"success": True, "data": result, "message": "Branch créée"}


@router.get("/{org_id}/branches", summary="Lister les branches")
async def list_branches(
    org_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    active_only: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    _require_branch_admin(current_user)
    _check_org_access(current_user, org_id)
    svc = BranchService(db)
    result = await svc.list(
        caller_org_id=current_user.org_id,
        org_id=org_id,
        page=page,
        page_size=page_size,
        active_only=active_only,
    )
    return {"success": True, "data": result}


@router.get("/{org_id}/branches/{branch_id}", summary="Détail d'une branch")
async def get_branch(
    org_id: str,
    branch_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    _require_branch_admin(current_user)
    _check_org_access(current_user, org_id)
    await assert_branch_access(db, current_user, org_id, branch_id)
    svc = BranchService(db)
    result = await svc.get_by_id(branch_id, current_user.org_id)
    return {"success": True, "data": result}


@router.patch("/{org_id}/branches/{branch_id}", summary="Mettre à jour une branch")
async def update_branch(
    org_id: str,
    branch_id: str,
    body: UpdateBranchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    _require_branch_admin(current_user)
    _check_org_access(current_user, org_id)
    await assert_branch_access(db, current_user, org_id, branch_id)
    svc = BranchService(db)
    result = await svc.update(branch_id, body, current_user.org_id)
    return {"success": True, "data": result, "message": "Branch mise à jour"}


@router.delete("/{org_id}/branches/{branch_id}", summary="Désactiver une branch")
async def delete_branch(
    org_id: str,
    branch_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    _require_org_admin(current_user)
    _check_org_access(current_user, org_id)
    svc = BranchService(db)
    await svc.delete(branch_id, current_user.org_id)
    return {"success": True, "message": "Branch désactivée"}


# ── Configuration file d'attente (S2-10) ──────────────────────────────────────

@router.get(
    "/{org_id}/branches/{branch_id}/config",
    summary="Configuration file d'attente d'une branch",
)
async def get_branch_config(
    org_id: str,
    branch_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """
    Retourne la configuration complète de la file d'attente.
    Accessible aux administrateurs de la branch.
    """
    _require_branch_admin(current_user)
    _check_org_access(current_user, org_id)
    await assert_branch_access(db, current_user, org_id, branch_id)
    svc = BranchService(db)
    result = await svc.get_config(branch_id, current_user.org_id)
    return {"success": True, "data": result}


@router.patch(
    "/{org_id}/branches/{branch_id}/config",
    summary="Modifier la configuration file d'attente",
)
async def update_branch_config(
    org_id: str,
    branch_id: str,
    body: UpdateBranchConfigRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """
    Met à jour la configuration de la file d'attente (PATCH partiel).
    Seuls les champs fournis sont modifiés.
    Réservé aux ADMIN_AGENCY (leur branch), ADMIN_ORG et SUPER_ADMIN.
    """
    _require_branch_admin(current_user)
    _check_org_access(current_user, org_id)
    await assert_branch_access(db, current_user, org_id, branch_id)
    svc = BranchService(db)
    result = await svc.update_config(branch_id, body, current_user.org_id)
    return {"success": True, "data": result, "message": "Configuration mise à jour"}


# ── Alias rétro-compatibles /agencies ─────────────────────────────────────────
# Maintenir les anciennes routes jusqu'à coordination avec le mobile (DÉCISION 8).
# À supprimer en Sprint 3 après migration mobile.

@router.post("/{org_id}/agencies", status_code=201, include_in_schema=False)
async def create_branch_compat(
    org_id: str,
    body: CreateBranchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Alias rétro-compatible — préférer /branches."""
    return await create_branch(org_id, body, db, current_user)


@router.get("/{org_id}/agencies", include_in_schema=False)
async def list_branches_compat(
    org_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    active_only: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Alias rétro-compatible — préférer /branches."""
    return await list_branches(org_id, page, page_size, active_only, db, current_user)


@router.get("/{org_id}/agencies/{branch_id}", include_in_schema=False)
async def get_branch_compat(
    org_id: str,
    branch_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Alias rétro-compatible — préférer /branches."""
    return await get_branch(org_id, branch_id, db, current_user)


@router.patch("/{org_id}/agencies/{branch_id}", include_in_schema=False)
async def update_branch_compat(
    org_id: str,
    branch_id: str,
    body: UpdateBranchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Alias rétro-compatible — préférer /branches."""
    return await update_branch(org_id, branch_id, body, db, current_user)


@router.delete("/{org_id}/agencies/{branch_id}", include_in_schema=False)
async def delete_branch_compat(
    org_id: str,
    branch_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Alias rétro-compatible — préférer /branches."""
    return await delete_branch(org_id, branch_id, db, current_user)
