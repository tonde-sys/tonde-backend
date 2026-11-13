"""
ExportService — export CSV / XLSX des tickets et des statistiques.

Points d'attention :

  * STREAMING : un export peut atteindre EXPORT_MAX_ROWS lignes (100 000). Le CSV est
    produit par lots (pagination par clé, EXPORT_BATCH_SIZE lignes) et envoyé au fil de
    l'eau : la mémoire reste constante. Le XLSX est construit en mode « write-only »
    d'openpyxl puis envoyé en un bloc.
  * SESSION PROPRE : FastAPI ferme la session de la requête AVANT la fin d'un
    StreamingResponse. Le générateur ouvre donc sa propre session.
  * INJECTION CSV : une cellule commençant par = + - @ est interprétée comme une formule
    par Excel (« =HYPERLINK(...) » exfiltre des données). Elle est préfixée d'une apostrophe.
  * AUDIT : chaque export réussi est tracé dans audit_logs (qui, quand, quels filtres).
    Un export interrompu avant la fin n'est PAS tracé comme réussi.
  * PÉRIMÈTRE : toujours borné à une organisation, et pour les rôles de branche, aux
    branches de l'employé (`branch_ids`).
"""
import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator, Callable

from fastapi import HTTPException
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.audit_log import AuditAction
from app.models.branch import Branch, Service
from app.models.ticket import Ticket, TicketStatus
from app.models.user import User
from app.services.analytics_service import AnalyticsService
from app.services.audit_service import AuditService
from app.services.ticket_service import as_utc

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]

CSV_MEDIA_TYPE = "text/csv; charset=utf-8"
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

TICKET_COLUMNS = [
    "ticket_id", "number", "status", "priority", "branch", "service", "counter",
    "created_at", "called_at", "served_at", "done_at",
    "wait_seconds", "service_seconds", "estimated_wait_minutes",
]
STATS_COLUMNS = ["service_id", "service_name", "total_tickets", "avg_wait_seconds", "absence_rate_pct"]

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


@dataclass
class TicketExportFilters:
    """Filtres d'un export de tickets. org_id est TOUJOURS fourni (isolation multi-tenant)."""
    org_id: str
    branch_ids: set[str] | None = None      # None = toute l'organisation
    service_id: str | None = None
    statuses: list[TicketStatus] | None = None
    from_date: datetime | None = None
    to_date: datetime | None = None
    limit: int = field(default_factory=lambda: settings.EXPORT_MAX_ROWS)

    def effective_limit(self) -> int:
        """Plafonne la limite demandée à EXPORT_MAX_ROWS (une valeur plus grande est ignorée)."""
        return max(1, min(self.limit, settings.EXPORT_MAX_ROWS))

    def describe(self) -> dict:
        """Filtres sérialisables, pour l'audit."""
        return {
            "branch_ids": sorted(self.branch_ids) if self.branch_ids is not None else None,
            "service_id": self.service_id,
            "statuses": [s.value for s in self.statuses] if self.statuses else None,
            "from_date": self.from_date.isoformat() if self.from_date else None,
            "to_date": self.to_date.isoformat() if self.to_date else None,
            "limit": self.effective_limit(),
        }


def safe_cell(value):
    """Neutralise l'injection de formule : texte commençant par = + - @ → préfixé d'une apostrophe."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def parse_statuses(raw: str | None) -> list[TicketStatus] | None:
    """
    Convertit « waiting,done » en liste de TicketStatus.

    Raises:
        HTTPException 422 INVALID_STATUS: valeur inconnue
    """
    if not raw:
        return None
    result = []
    for part in raw.split(","):
        part = part.strip().lower()
        if not part:
            continue
        try:
            result.append(TicketStatus(part))
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "INVALID_STATUS",
                    "message": f"Statut inconnu : '{part}'. Valeurs : {', '.join(s.value for s in TicketStatus)}",
                },
            ) from None
    return result or None


def _iso(value: datetime | None) -> str:
    return as_utc(value).isoformat() if value else ""


class ExportService:

    def __init__(self, session_factory: SessionFactory = AsyncSessionLocal) -> None:
        self._session_factory = session_factory

    # ── Tickets ───────────────────────────────────────────────────────────────

    async def stream_tickets_csv(self, filters: TicketExportFilters, actor: User) -> AsyncIterator[bytes]:
        """
        Génère l'export CSV des tickets, lot par lot (mémoire constante).

        Le fichier est encodé UTF-8 avec BOM (Excel reconnaît ainsi les accents), séparateur
        virgule, en-tête en première ligne. L'audit n'est écrit qu'APRÈS le dernier lot.
        """
        actor_id, actor_role = actor.id, actor.role.value  # lus avant le streaming

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(TICKET_COLUMNS)
        yield ("\ufeff" + buffer.getvalue()).encode("utf-8")

        rows_written = 0
        async for batch in self._ticket_batches(filters):
            buffer.seek(0)
            buffer.truncate(0)
            for row in batch:
                writer.writerow([safe_cell(v) for v in row])
            rows_written += len(batch)
            yield buffer.getvalue().encode("utf-8")

        await self._audit(filters, actor_id, actor_role, AuditAction.TICKET_EXPORTED, "csv", rows_written)

    async def build_tickets_xlsx(self, filters: TicketExportFilters, actor: User) -> bytes:
        """
        Construit l'export XLSX des tickets (feuille « Tickets », en-tête en gras).

        openpyxl en mode write-only : les lignes sont écrites au fil de l'eau dans le classeur,
        sans le matérialiser en mémoire sous forme d'objets cellules.
        """
        actor_id, actor_role = actor.id, actor.role.value

        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet("Tickets")
        sheet.append(self._header_cells(sheet, TICKET_COLUMNS))

        rows_written = 0
        async for batch in self._ticket_batches(filters):
            for row in batch:
                sheet.append([safe_cell(v) for v in row])
            rows_written += len(batch)

        payload = self._save(workbook)
        await self._audit(filters, actor_id, actor_role, AuditAction.TICKET_EXPORTED, "xlsx", rows_written)
        return payload

    async def _ticket_batches(self, filters: TicketExportFilters) -> AsyncIterator[list[list]]:
        """
        Parcourt les tickets par lots avec une pagination PAR CLÉ (created_at, id).

        Une pagination par OFFSET ralentit à chaque page sur de gros volumes ; la clé
        (created_at, id) garde un coût constant et un ordre stable.
        """
        remaining = filters.effective_limit()
        last_key: tuple | None = None

        async with self._session_factory() as session:
            while remaining > 0:
                query = (
                    select(Ticket, Service.name.label("service_name"), Branch.name.label("branch_name"))
                    .join(Service, Service.id == Ticket.service_id)
                    .join(Branch, Branch.id == Ticket.agency_id)
                    .where(Ticket.org_id == filters.org_id)
                    .order_by(Ticket.created_at.asc(), Ticket.id.asc())
                    .limit(min(settings.EXPORT_BATCH_SIZE, remaining))
                )
                if filters.branch_ids is not None:
                    query = query.where(Ticket.agency_id.in_(filters.branch_ids))
                if filters.service_id:
                    query = query.where(Ticket.service_id == filters.service_id)
                if filters.statuses:
                    query = query.where(Ticket.status.in_(filters.statuses))
                if filters.from_date:
                    query = query.where(Ticket.created_at >= as_utc(filters.from_date))
                if filters.to_date:
                    query = query.where(Ticket.created_at <= as_utc(filters.to_date))
                if last_key is not None:
                    query = query.where(tuple_(Ticket.created_at, Ticket.id) > tuple_(*last_key))

                rows = (await session.execute(query)).all()
                if not rows:
                    return
                last = rows[-1].Ticket
                last_key = (last.created_at, last.id)
                remaining -= len(rows)
                yield [self._ticket_row(r.Ticket, r.service_name, r.branch_name) for r in rows]

    @staticmethod
    def _ticket_row(ticket: Ticket, service_name: str, branch_name: str) -> list:
        wait_seconds = (
            int((as_utc(ticket.called_at) - as_utc(ticket.created_at)).total_seconds())
            if ticket.called_at else ""
        )
        return [
            ticket.id, ticket.number, ticket.status.value, ticket.priority.value,
            branch_name, service_name, ticket.counter_name or "",
            _iso(ticket.created_at), _iso(ticket.called_at), _iso(ticket.served_at), _iso(ticket.done_at),
            wait_seconds,
            ticket.service_seconds if ticket.service_seconds is not None else "",
            ticket.estimated_wait_minutes if ticket.estimated_wait_minutes is not None else "",
        ]

    # ── Statistiques d'une branch ─────────────────────────────────────────────

    async def build_stats(
        self,
        org_id: str,
        branch_id: str,
        fmt: str,
        actor: User,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
    ) -> bytes:
        """
        Exporte les statistiques agrégées d'une branch (depuis queue_logs).

        CSV : une ligne par service (service_id, service_name, total_tickets,
        avg_wait_seconds, absence_rate_pct). XLSX : feuille « Résumé » (métriques
        globales) et feuille « Par service ». Sans donnée : fichier avec l'en-tête seul.

        Args:
            org_id: Organisation (déjà vérifiée par le router)
            branch_id: Branch (déjà vérifiée par le router)
            fmt: "csv" ou "xlsx"
            actor: Utilisateur demandeur (audit)
            from_date / to_date: Période (défaut : 7 derniers jours)
        """
        actor_id, actor_role = actor.id, actor.role.value
        async with self._session_factory() as session:
            analytics = AnalyticsService(session)
            summary = await analytics.get_agency_summary(org_id, branch_id, from_date, to_date)
            per_service = await analytics.get_service_stats(org_id, branch_id, from_date, to_date)

        service_rows = [
            [s["service_id"], s["service_name"], s["total_tickets"], s["avg_wait_seconds"], s["absence_rate_percent"]]
            for s in per_service
        ]

        if fmt == "csv":
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(STATS_COLUMNS)
            for row in service_rows:
                writer.writerow([safe_cell(v) for v in row])
            payload = ("\ufeff" + buffer.getvalue()).encode("utf-8")
        else:
            workbook = Workbook(write_only=True)
            summary_sheet = workbook.create_sheet("Résumé")
            summary_sheet.append(self._header_cells(summary_sheet, ["metric", "value"]))
            for key in ("total_tickets", "avg_wait_minutes", "absence_rate_percent",
                        "total_called", "total_absent", "busiest_hour"):
                summary_sheet.append([key, summary.get(key)])
            for status_name, count in summary.get("tickets_by_status", {}).items():
                summary_sheet.append([f"status_{status_name}", count])
            service_sheet = workbook.create_sheet("Par service")
            service_sheet.append(self._header_cells(service_sheet, STATS_COLUMNS))
            for row in service_rows:
                service_sheet.append([safe_cell(v) for v in row])
            payload = self._save(workbook)

        async with self._session_factory() as session:
            await AuditService(session).log(
                org_id=org_id, action=AuditAction.STATS_EXPORTED,
                actor_id=actor_id, actor_role=actor_role,
                target_type="branch", target_id=branch_id,
                extra={"format": fmt, "services": len(service_rows),
                       "from_date": from_date.isoformat() if from_date else None,
                       "to_date": to_date.isoformat() if to_date else None},
            )
        return payload

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _header_cells(sheet, names: list[str]) -> list[WriteOnlyCell]:
        """Ligne d'en-tête en gras (mode write-only : les cellules se construisent à part)."""
        cells = []
        for name in names:
            cell = WriteOnlyCell(sheet, value=name)
            cell.font = Font(bold=True)
            cells.append(cell)
        return cells

    @staticmethod
    def _save(workbook: Workbook) -> bytes:
        stream = io.BytesIO()
        workbook.save(stream)
        return stream.getvalue()

    async def _audit(
        self, filters: TicketExportFilters, actor_id: str, actor_role: str,
        action: str, fmt: str, rows: int,
    ) -> None:
        """Trace un export RÉUSSI. Un échec d'audit est journalisé mais n'invalide pas l'export déjà livré."""
        try:
            async with self._session_factory() as session:
                await AuditService(session).log(
                    org_id=filters.org_id, action=action,
                    actor_id=actor_id, actor_role=actor_role,
                    target_type="organization", target_id=filters.org_id,
                    extra={**filters.describe(), "format": fmt, "rows": rows},
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Audit d'export non écrit (org={filters.org_id}) : {exc}")
