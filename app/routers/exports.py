"""
Router Exports — export CSV / XLSX des données (S3-01).

  GET /organizations/{org_id}/tickets/export
  GET /organizations/{org_id}/branches/{branch_id}/tickets/export
  GET /organizations/{org_id}/branches/{branch_id}/stats/export

Permissions : SUPERVISOR et plus.
  - ADMIN_ORG / SUPER_ADMIN : toute l'organisation.
  - SUPERVISOR / ADMIN_AGENCY : uniquement leurs branches.
Chaque export réussi est tracé dans le journal d'audit.
"""
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import ORG_WIDE_ROLES, assert_branch_access, assert_org_access, forbidden, get_staff_branch_ids
from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_supervisor
from app.models.user import User
from app.services.export_service import (
    CSV_MEDIA_TYPE,
    XLSX_MEDIA_TYPE,
    ExportService,
    TicketExportFilters,
    parse_statuses,
)

router = APIRouter()


def _validate_format(fmt: str) -> str:
    """Seuls csv et xlsx sont acceptés (422 INVALID_FORMAT sinon)."""
    fmt = (fmt or "").lower()
    if fmt not in ("csv", "xlsx"):
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_FORMAT", "message": "Format invalide : utilisez 'csv' ou 'xlsx'"},
        )
    return fmt


def _validate_period(from_date: datetime | None, to_date: datetime | None) -> None:
    if from_date and to_date and from_date > to_date:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_PERIOD", "message": "from_date doit précéder to_date"},
        )


def _attachment(prefix: str, org_id: str, fmt: str) -> dict[str, str]:
    """En-tête Content-Disposition : {prefix}_{org_id}_{date}.{ext}"""
    return {"Content-Disposition": f'attachment; filename="{prefix}_{org_id}_{date.today().isoformat()}.{fmt}"'}


async def _export_tickets(
    *,
    db: AsyncSession,
    user: User,
    org_id: str,
    branch_ids: set[str] | None,
    fmt: str,
    service_id: str | None,
    status: str | None,
    from_date: datetime | None,
    to_date: datetime | None,
    limit: int,
):
    """Construit la réponse d'export de tickets (CSV en streaming, XLSX en un bloc)."""
    filters = TicketExportFilters(
        org_id=org_id,
        branch_ids=branch_ids,
        service_id=service_id,
        statuses=parse_statuses(status),
        from_date=from_date,
        to_date=to_date,
        limit=limit,
    )
    service = ExportService()
    headers = _attachment("tickets", org_id, fmt)
    if fmt == "csv":
        return StreamingResponse(
            service.stream_tickets_csv(filters, user), media_type=CSV_MEDIA_TYPE, headers=headers
        )
    return Response(
        content=await service.build_tickets_xlsx(filters, user), media_type=XLSX_MEDIA_TYPE, headers=headers
    )


@router.get("/{org_id}/tickets/export", summary="Exporter les tickets d'une organisation")
async def export_org_tickets(
    org_id: str,
    format: str = Query("csv", description="csv ou xlsx"),
    branch_id: str | None = Query(None, description="Restreindre à une branch"),
    service_id: str | None = Query(None),
    status: str | None = Query(None, description="Statuts séparés par des virgules (ex. waiting,done)"),
    from_date: datetime | None = Query(None, description="created_at ≥ (ISO 8601)"),
    to_date: datetime | None = Query(None, description="created_at ≤ (ISO 8601)"),
    limit: int = Query(settings.EXPORT_MAX_ROWS, ge=1, description="Plafonné à 100 000"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_supervisor),
):
    """
    Exporte les tickets de l'organisation (CSV en streaming ou XLSX).

    Un superviseur ou admin d'agence n'obtient que les tickets de SES branches.
    """
    fmt = _validate_format(format)
    _validate_period(from_date, to_date)
    assert_org_access(current_user, org_id)

    branch_ids: set[str] | None = None
    if current_user.role not in ORG_WIDE_ROLES:
        branch_ids = await get_staff_branch_ids(db, current_user)
        if not branch_ids:
            raise forbidden("Vous n'êtes affecté à aucune agence", code="BRANCH_FORBIDDEN")
        if branch_id and branch_id not in branch_ids:
            raise forbidden("Vous n'êtes pas affecté à cette agence", code="BRANCH_FORBIDDEN")
    if branch_id:
        await assert_branch_access(db, current_user, org_id, branch_id)  # 404 si hors organisation
        branch_ids = {branch_id}

    return await _export_tickets(
        db=db, user=current_user, org_id=org_id, branch_ids=branch_ids, fmt=fmt,
        service_id=service_id, status=status, from_date=from_date, to_date=to_date, limit=limit,
    )


@router.get("/{org_id}/branches/{branch_id}/tickets/export", summary="Exporter les tickets d'une branch")
async def export_branch_tickets(
    org_id: str,
    branch_id: str,
    format: str = Query("csv", description="csv ou xlsx"),
    service_id: str | None = Query(None),
    status: str | None = Query(None, description="Statuts séparés par des virgules"),
    from_date: datetime | None = Query(None),
    to_date: datetime | None = Query(None),
    limit: int = Query(settings.EXPORT_MAX_ROWS, ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_supervisor),
):
    """Exporte les tickets d'UNE branch. 403 si le demandeur n'y est pas affecté."""
    fmt = _validate_format(format)
    _validate_period(from_date, to_date)
    await assert_branch_access(db, current_user, org_id, branch_id)
    return await _export_tickets(
        db=db, user=current_user, org_id=org_id, branch_ids={branch_id}, fmt=fmt,
        service_id=service_id, status=status, from_date=from_date, to_date=to_date, limit=limit,
    )


@router.get("/{org_id}/branches/{branch_id}/stats/export", summary="Exporter les statistiques d'une branch")
async def export_branch_stats(
    org_id: str,
    branch_id: str,
    format: str = Query("csv", description="csv ou xlsx"),
    from_date: datetime | None = Query(None),
    to_date: datetime | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_supervisor),
):
    """
    Statistiques agrégées d'une branch (depuis queue_logs).
    CSV : une ligne par service. XLSX : feuilles « Résumé » et « Par service ».
    """
    fmt = _validate_format(format)
    _validate_period(from_date, to_date)
    await assert_branch_access(db, current_user, org_id, branch_id)
    payload = await ExportService().build_stats(org_id, branch_id, fmt, current_user, from_date, to_date)
    return Response(
        content=payload,
        media_type=CSV_MEDIA_TYPE if fmt == "csv" else XLSX_MEDIA_TYPE,
        headers=_attachment("stats", branch_id, fmt),
    )
