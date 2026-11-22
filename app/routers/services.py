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
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import assert_branch_access
from app.core.database import get_db
from app.core.deps import get_current_admin_agency
from app.models.user import User
from app.models.eta import EtaCounterProfile, EtaServiceProfile
from app.models.branch import Service
from app.schemas.service import CreateServiceRequest, UpdateServiceRequest
from app.services.service_service import ServiceService
from app.services.eta_service import EtaEstimator

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


@router.get(
    "/organizations/{org_id}/branches/{branch_id}/services/{service_id}/eta/metrics",
    summary="Consulter la configuration AWTE et sa précision",
)
async def eta_metrics(
    org_id: str,
    branch_id: str,
    service_id: str,
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_agency),
) -> dict:
    """Configuration, profils EWMA et MAE, accessibles uniquement au staff admin."""
    await assert_branch_access(db, current_user, org_id, branch_id)
    service = (await db.execute(
        select(Service).where(
            Service.id == service_id,
            Service.org_id == org_id,
            Service.branch_id == branch_id,
        )
    )).scalar_one_or_none()
    if service is None:
        raise HTTPException(status_code=404, detail={"code": "SERVICE_NOT_FOUND", "message": "Service introuvable"})

    service_profile = await db.get(EtaServiceProfile, service.id)
    counter_profiles = (await db.execute(
        select(EtaCounterProfile).where(
            EtaCounterProfile.org_id == org_id,
            EtaCounterProfile.service_id == service_id,
        ).order_by(EtaCounterProfile.counter_id)
    )).scalars().all()
    mae = await EtaEstimator(db).metrics(
        org_id=org_id, service_id=service_id, since=since, until=until
    )
    return {
        "success": True,
        "data": {
            "org_id": org_id,
            "branch_id": branch_id,
            "service_id": service_id,
            "configuration": {
                "initial_duration_minutes": service.avg_duration_minutes,
                "ewma_alpha": service.eta_ewma_alpha,
                "minimum_service_samples": service.eta_min_service_samples,
                "minimum_counter_samples": service.eta_min_counter_samples,
                "outlier_minimum_samples": service.eta_outlier_min_samples,
                "outlier_mad_multiplier": service.eta_outlier_mad_multiplier,
            },
            "service_profile": {
                "ewma_seconds": service_profile.ewma_seconds if service_profile else service.avg_duration_minutes * 60,
                "sample_count": service_profile.sample_count if service_profile else 0,
                "level_active": bool(service_profile and service_profile.sample_count >= service.eta_min_service_samples),
            },
            "counter_profiles": [
                {
                    "counter_id": profile.counter_id,
                    "ewma_seconds": profile.ewma_seconds,
                    "sample_count": profile.sample_count,
                    "level_active": profile.sample_count >= service.eta_min_counter_samples,
                }
                for profile in counter_profiles
            ],
            "waiting_time_accuracy": mae,
        },
    }


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

    Passe par `PublicQueueService` et non par `ServiceService` : le mobile a
    besoin du nombre de personnes en attente par service pour annoncer
    l'attente avant l'entrée en file, et ce compte se lit en base.
    """
    from app.services.public_service import PublicQueueService

    return {"success": True, "data": await PublicQueueService(db).list_services(branch_id)}
