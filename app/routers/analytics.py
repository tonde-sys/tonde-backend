"""
Router Analytics — /api/v1/organizations/{org_id}/agencies/{agency_id}/analytics/...

Endpoints de consultation des métriques calculées depuis queue_logs.
Réservés aux rôles SUPERVISOR minimum (agents ne voient pas les stats globales).

Toutes les routes filtrent par org_id — isolation multi-tenant absolue.

DÉCISION 9 — Feuille de route IA :
  MVP → Collecte (S2-04) → Analytics (S2-06) → Rules Engine → ML → IA
"""
import logging
from datetime import datetime
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import assert_branch_access
from app.core.database import get_db
from app.core.deps import get_current_supervisor
from app.models.user import User
from app.services.analytics_service import AnalyticsService

logger = logging.getLogger(__name__)
router = APIRouter()


# Contrôle d'accès : assert_branch_access vérifie l'organisation ET la branch
# (un superviseur de la branch A ne lit pas les stats de la branch B).

# ── Résumé général ────────────────────────────────────────────────────────────

@router.get("/{org_id}/branches/{agency_id}/analytics/summary", include_in_schema=False)
@router.get(
    "/{org_id}/agencies/{agency_id}/analytics/summary",
    summary="Résumé des métriques d'une agence",
)
async def get_agency_summary(
    org_id: str,
    agency_id: str,
    date_from: datetime | None = Query(default=None, description="Début de période (ISO 8601)"),
    date_to: datetime | None = Query(default=None, description="Fin de période (ISO 8601)"),
    current_user: User = Depends(get_current_supervisor),
    db: AsyncSession = Depends(get_db),
):
    """
    Résumé des métriques clés d'une agence sur une période donnée.

    Retourne : total tickets, temps d'attente moyen, taux d'absence,
    répartition par statut final, heure de pointe.

    Par défaut : 7 derniers jours.
    Nécessite rôle SUPERVISOR minimum.
    """
    await assert_branch_access(db, current_user, org_id, agency_id)
    svc = AnalyticsService(db)
    result = await svc.get_agency_summary(
        org_id=org_id,
        agency_id=agency_id,
        date_from=date_from,
        date_to=date_to,
    )
    return {"success": True, "data": result}


# ── Stats par service ─────────────────────────────────────────────────────────

@router.get("/{org_id}/branches/{agency_id}/analytics/services", include_in_schema=False)
@router.get(
    "/{org_id}/agencies/{agency_id}/analytics/services",
    summary="Statistiques par service",
)
async def get_service_stats(
    org_id: str,
    agency_id: str,
    date_from: datetime | None = Query(default=None, description="Début de période (ISO 8601)"),
    date_to: datetime | None = Query(default=None, description="Fin de période (ISO 8601)"),
    current_user: User = Depends(get_current_supervisor),
    db: AsyncSession = Depends(get_db),
):
    """
    Métriques par service (Caisse, Crédit, etc.) au sein d'une agence.

    Permet de comparer la charge et les temps d'attente entre services.
    Nécessite rôle SUPERVISOR minimum.
    """
    await assert_branch_access(db, current_user, org_id, agency_id)
    svc = AnalyticsService(db)
    items = await svc.get_service_stats(
        org_id=org_id,
        agency_id=agency_id,
        date_from=date_from,
        date_to=date_to,
    )
    return {
        "success": True,
        "data": {
            "agency_id": agency_id,
            "services": items,
        },
    }


# ── Performance par guichet ───────────────────────────────────────────────────

@router.get("/{org_id}/branches/{agency_id}/analytics/counters", include_in_schema=False)
@router.get(
    "/{org_id}/agencies/{agency_id}/analytics/counters",
    summary="Performance par guichet",
)
async def get_counter_performance(
    org_id: str,
    agency_id: str,
    date_from: datetime | None = Query(default=None, description="Début de période (ISO 8601)"),
    date_to: datetime | None = Query(default=None, description="Fin de période (ISO 8601)"),
    current_user: User = Depends(get_current_supervisor),
    db: AsyncSession = Depends(get_db),
):
    """
    Tickets traités par guichet sur la période.

    Permet d'évaluer la productivité par agent et d'équilibrer la charge.
    Nécessite rôle SUPERVISOR minimum.
    """
    await assert_branch_access(db, current_user, org_id, agency_id)
    svc = AnalyticsService(db)
    items = await svc.get_counter_performance(
        org_id=org_id,
        agency_id=agency_id,
        date_from=date_from,
        date_to=date_to,
    )
    return {
        "success": True,
        "data": {
            "agency_id": agency_id,
            "counters": items,
        },
    }


# ── Distribution horaire ──────────────────────────────────────────────────────

@router.get("/{org_id}/branches/{agency_id}/analytics/hourly", include_in_schema=False)
@router.get(
    "/{org_id}/agencies/{agency_id}/analytics/hourly",
    summary="Distribution horaire du volume de tickets",
)
async def get_hourly_distribution(
    org_id: str,
    agency_id: str,
    date_from: datetime | None = Query(default=None, description="Début de période (ISO 8601)"),
    date_to: datetime | None = Query(default=None, description="Fin de période (ISO 8601)"),
    current_user: User = Depends(get_current_supervisor),
    db: AsyncSession = Depends(get_db),
):
    """
    Distribution du volume de tickets par heure de la journée (0-23).

    Permet d'identifier les pics horaires pour optimiser le staffing.
    Retourne toujours 24 entrées, même si certaines heures ont 0 ticket.
    Nécessite rôle SUPERVISOR minimum.
    """
    await assert_branch_access(db, current_user, org_id, agency_id)
    svc = AnalyticsService(db)
    items = await svc.get_hourly_distribution(
        org_id=org_id,
        agency_id=agency_id,
        date_from=date_from,
        date_to=date_to,
    )
    return {
        "success": True,
        "data": {
            "agency_id": agency_id,
            "distribution": items,
        },
    }
