"""
AnalyticsService — Pipeline analytics de base (S2-06).

Source de données : table `queue_logs` (S2-04).
Toutes les métriques sont calculées à la demande depuis les logs immuables.

Métriques disponibles :
  - Temps d'attente moyen par agence / service / heure
  - Taux d'absence (ABSENT / total CALLED)
  - Tickets traités par agent (counter)
  - Volume de tickets par statut final
  - Pics horaires (distribution par heure de la journée)

Règle multi-tenant absolue : toutes les requêtes filtrent par org_id.
Aucune donnée d'une organisation n'est accessible depuis une autre.

DÉCISION 9 — Feuille de route IA :
  MVP → Collecte (S2-04) → Analytics (S2-06) → Rules Engine → ML → IA
  Ce service est l'étape 2 de cette progression.
"""
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_

from app.models.branch import Service
from app.models.queue_log import QueueLog, QueueLogAction

logger = logging.getLogger(__name__)


class AnalyticsService:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ── Résumé général d'une agence ───────────────────────────────────────────
    async def get_agency_summary(
        self,
        org_id: str,
        agency_id: str,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> dict:
        """
        Résumé des métriques clés d'une agence sur une période donnée.

        Par défaut, retourne les données des 7 derniers jours.
        Toutes les métriques sont calculées depuis queue_logs.

        Args:
            org_id: ID de l'organisation (isolation multi-tenant — obligatoire)
            agency_id: ID de l'agence cible
            date_from: Début de la période (défaut: aujourd'hui - 7 jours)
            date_to: Fin de la période (défaut: maintenant)

        Returns:
            Dict avec total_tickets, avg_wait_seconds, absence_rate,
            tickets_by_status, busiest_hour
        """
        date_from, date_to = self._resolve_date_range(date_from, date_to)

        base_filter = and_(
            QueueLog.org_id == org_id,
            QueueLog.agency_id == agency_id,
            QueueLog.created_at >= date_from,
            QueueLog.created_at <= date_to,
        )

        # ── Total tickets créés ───────────────────────────────
        total_result = await self.db.execute(
            select(func.count(QueueLog.id)).where(
                base_filter,
                QueueLog.action == QueueLogAction.TICKET_CREATED,
            )
        )
        total_tickets = total_result.scalar_one() or 0

        # ── Temps d'attente moyen (WAITING → CALLED) ─────────
        # elapsed_seconds au moment de TICKET_CALLED = temps passé en file
        avg_wait_result = await self.db.execute(
            select(func.avg(QueueLog.elapsed_seconds)).where(
                base_filter,
                QueueLog.action == QueueLogAction.TICKET_CALLED,
                QueueLog.elapsed_seconds.isnot(None),
            )
        )
        avg_wait_seconds = float(avg_wait_result.scalar_one() or 0)

        # ── Taux d'absence ────────────────────────────────────
        called_result = await self.db.execute(
            select(func.count(QueueLog.id)).where(
                base_filter,
                QueueLog.action == QueueLogAction.TICKET_CALLED,
            )
        )
        total_called = called_result.scalar_one() or 0

        absent_result = await self.db.execute(
            select(func.count(QueueLog.id)).where(
                base_filter,
                QueueLog.action == QueueLogAction.TICKET_ABSENT,
            )
        )
        total_absent = absent_result.scalar_one() or 0

        absence_rate = (total_absent / total_called * 100) if total_called > 0 else 0.0

        # ── Tickets par statut final ──────────────────────────
        tickets_by_status = await self._get_tickets_by_final_status(
            org_id, agency_id, date_from, date_to
        )

        # ── Heure de pointe ───────────────────────────────────
        busiest_hour = await self._get_busiest_hour(org_id, agency_id, date_from, date_to)

        return {
            "org_id": org_id,
            "agency_id": agency_id,
            "period": {
                "from": date_from.isoformat(),
                "to": date_to.isoformat(),
            },
            "total_tickets": total_tickets,
            "avg_wait_minutes": round(avg_wait_seconds / 60, 1),
            "absence_rate_percent": round(absence_rate, 1),
            "total_called": total_called,
            "total_absent": total_absent,
            "tickets_by_status": tickets_by_status,
            "busiest_hour": busiest_hour,
        }

    # ── Métriques par service ─────────────────────────────────────────────────
    async def get_service_stats(
        self,
        org_id: str,
        agency_id: str,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[dict]:
        """
        Métriques détaillées par service au sein d'une agence.

        Permet de comparer la charge entre services (Caisse, Crédit, etc.)
        et d'identifier les goulots d'étranglement.

        Args:
            org_id: ID de l'organisation
            agency_id: ID de l'agence
            date_from: Début de la période
            date_to: Fin de la période

        Returns:
            Liste de dicts, un par service, avec volume et temps moyen
        """
        date_from, date_to = self._resolve_date_range(date_from, date_to)

        # Volume de tickets créés par service
        volume_result = await self.db.execute(
            select(
                QueueLog.service_id,
                func.count(QueueLog.id).label("total"),
            )
            .where(
                QueueLog.org_id == org_id,
                QueueLog.agency_id == agency_id,
                QueueLog.action == QueueLogAction.TICKET_CREATED,
                QueueLog.created_at >= date_from,
                QueueLog.created_at <= date_to,
            )
            .group_by(QueueLog.service_id)
        )
        volumes = {row.service_id: row.total for row in volume_result.fetchall()}

        # Temps d'attente moyen par service
        wait_result = await self.db.execute(
            select(
                QueueLog.service_id,
                func.avg(QueueLog.elapsed_seconds).label("avg_wait"),
            )
            .where(
                QueueLog.org_id == org_id,
                QueueLog.agency_id == agency_id,
                QueueLog.action == QueueLogAction.TICKET_CALLED,
                QueueLog.elapsed_seconds.isnot(None),
                QueueLog.created_at >= date_from,
                QueueLog.created_at <= date_to,
            )
            .group_by(QueueLog.service_id)
        )
        avg_waits = {row.service_id: float(row.avg_wait or 0) for row in wait_result.fetchall()}

        # Appels et absences par service → taux d'absence par service
        action_counts = await self.db.execute(
            select(
                QueueLog.service_id,
                QueueLog.action,
                func.count(QueueLog.id).label("total"),
            )
            .where(
                QueueLog.org_id == org_id,
                QueueLog.agency_id == agency_id,
                QueueLog.action.in_([QueueLogAction.TICKET_CALLED, QueueLogAction.TICKET_ABSENT]),
                QueueLog.created_at >= date_from,
                QueueLog.created_at <= date_to,
            )
            .group_by(QueueLog.service_id, QueueLog.action)
        )
        called: dict[str, int] = {}
        absent: dict[str, int] = {}
        for row in action_counts.fetchall():
            (called if row.action == QueueLogAction.TICKET_CALLED else absent)[row.service_id] = row.total

        # Agréger les résultats
        all_service_ids = set(volumes.keys()) | set(avg_waits.keys()) | set(called.keys())
        names: dict[str, str] = {}
        if all_service_ids:
            name_rows = await self.db.execute(
                select(Service.id, Service.name).where(Service.id.in_(all_service_ids))
            )
            names = {row.id: row.name for row in name_rows.fetchall()}

        stats = []
        for sid in sorted(all_service_ids):
            total_called = called.get(sid, 0)
            total_absent = absent.get(sid, 0)
            stats.append({
                "service_id": sid,
                "service_name": names.get(sid, ""),
                "total_tickets": volumes.get(sid, 0),
                "avg_wait_seconds": round(avg_waits.get(sid, 0)),
                "avg_wait_minutes": round(avg_waits.get(sid, 0) / 60, 1),
                "total_called": total_called,
                "total_absent": total_absent,
                "absence_rate_percent": round(total_absent / total_called * 100, 1) if total_called else 0.0,
            })
        return stats

    # ── Performances par guichet ──────────────────────────────────────────────
    async def get_counter_performance(
        self,
        org_id: str,
        agency_id: str,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[dict]:
        """
        Tickets traités par guichet (counter) sur la période.

        Métrique clé pour évaluer la productivité des agents
        et équilibrer la charge entre les guichets.

        Args:
            org_id: ID de l'organisation
            agency_id: ID de l'agence
            date_from: Début de la période
            date_to: Fin de la période

        Returns:
            Liste de dicts par counter_name avec tickets_done et avg_service_seconds
        """
        date_from, date_to = self._resolve_date_range(date_from, date_to)

        result = await self.db.execute(
            select(
                QueueLog.counter_id,
                QueueLog.counter_name,
                func.count(QueueLog.id).label("tickets_done"),
            )
            .where(
                QueueLog.org_id == org_id,
                QueueLog.agency_id == agency_id,
                QueueLog.action == QueueLogAction.TICKET_DONE,
                QueueLog.counter_id.isnot(None),
                QueueLog.created_at >= date_from,
                QueueLog.created_at <= date_to,
            )
            .group_by(QueueLog.counter_id, QueueLog.counter_name)
            .order_by(func.count(QueueLog.id).desc())
        )

        return [
            {
                "counter_id": row.counter_id,
                "counter_name": row.counter_name or "Inconnu",
                "tickets_done": row.tickets_done,
            }
            for row in result.fetchall()
        ]

    # ── Distribution horaire ──────────────────────────────────────────────────
    async def get_hourly_distribution(
        self,
        org_id: str,
        agency_id: str,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[dict]:
        """
        Distribution du volume de tickets par heure de la journée.

        Permet d'identifier les pics horaires et d'optimiser le staffing.
        Retourne 24 entrées (une par heure, 0-23).

        Args:
            org_id: ID de l'organisation
            agency_id: ID de l'agence
            date_from: Début de la période
            date_to: Fin de la période

        Returns:
            Liste de 24 dicts {hour, ticket_count}
        """
        date_from, date_to = self._resolve_date_range(date_from, date_to)

        result = await self.db.execute(
            select(
                func.extract("hour", QueueLog.created_at).label("hour"),
                func.count(QueueLog.id).label("count"),
            )
            .where(
                QueueLog.org_id == org_id,
                QueueLog.agency_id == agency_id,
                QueueLog.action == QueueLogAction.TICKET_CREATED,
                QueueLog.created_at >= date_from,
                QueueLog.created_at <= date_to,
            )
            .group_by(func.extract("hour", QueueLog.created_at))
            .order_by(func.extract("hour", QueueLog.created_at))
        )

        rows = {int(row.hour): row.count for row in result.fetchall()}

        # Retourner toutes les heures 0-23, même celles sans tickets
        return [
            {"hour": h, "ticket_count": rows.get(h, 0)}
            for h in range(24)
        ]

    # ── Helpers privés ────────────────────────────────────────────────────────
    def _resolve_date_range(
        self,
        date_from: datetime | None,
        date_to: datetime | None,
        default_days: int = 7,
    ) -> tuple[datetime, datetime]:
        """
        Résout la plage de dates. Par défaut : 7 derniers jours.

        Args:
            date_from: Date de début (None = aujourd'hui - default_days)
            date_to: Date de fin (None = maintenant)
            default_days: Nombre de jours par défaut si date_from est None

        Returns:
            Tuple (date_from, date_to) avec timezone UTC
        """
        now = datetime.now(timezone.utc)
        if date_to is None:
            date_to = now
        if date_from is None:
            date_from = now - timedelta(days=default_days)
        return date_from, date_to

    async def _get_tickets_by_final_status(
        self,
        org_id: str,
        agency_id: str,
        date_from: datetime,
        date_to: datetime,
    ) -> dict[str, int]:
        """
        Compte les tickets par statut final sur la période.
        Utilise les actions terminales dans queue_logs.
        """
        terminal_actions = [
            QueueLogAction.TICKET_DONE,
            QueueLogAction.TICKET_CANCELLED,
            QueueLogAction.TICKET_INCOMPLETE,
            QueueLogAction.TICKET_TRANSFERRED,
        ]

        result = await self.db.execute(
            select(
                QueueLog.action,
                func.count(QueueLog.id).label("count"),
            )
            .where(
                QueueLog.org_id == org_id,
                QueueLog.agency_id == agency_id,
                QueueLog.action.in_(terminal_actions),
                QueueLog.created_at >= date_from,
                QueueLog.created_at <= date_to,
            )
            .group_by(QueueLog.action)
        )

        return {row.action.value: row.count for row in result.fetchall()}

    async def _get_busiest_hour(
        self,
        org_id: str,
        agency_id: str,
        date_from: datetime,
        date_to: datetime,
    ) -> int | None:
        """
        Retourne l'heure (0-23) avec le plus grand volume de tickets créés.
        Retourne None si aucune donnée disponible.
        """
        result = await self.db.execute(
            select(
                func.extract("hour", QueueLog.created_at).label("hour"),
                func.count(QueueLog.id).label("count"),
            )
            .where(
                QueueLog.org_id == org_id,
                QueueLog.agency_id == agency_id,
                QueueLog.action == QueueLogAction.TICKET_CREATED,
                QueueLog.created_at >= date_from,
                QueueLog.created_at <= date_to,
            )
            .group_by(func.extract("hour", QueueLog.created_at))
            .order_by(func.count(QueueLog.id).desc())
            .limit(1)
        )
        row = result.fetchone()
        return int(row.hour) if row else None
