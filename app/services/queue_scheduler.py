"""
QueueScheduler — tâches de fond du Queue Engine (sans Celery).

Fonctions :
  1. Avertissement « présentez-vous » (ABSENT_WARNING_SECONDS après l'appel)
  2. Passage automatique CALLED → ABSENT (ABSENT_TIMEOUT_SECONDS après l'appel)
  3. Maintenance périodique : expiration des tickets bloqués (WAITING trop ancien,
     ABSENT hors fenêtre de retour, SERVING oublié, INCOMPLETE non résolu) et
     alertes d'attente longue (Rules Engine)

Conçu pour tourner sur PLUSIEURS instances de l'API en parallèle :
  - les échéances vivent dans des ZSET Redis ; ZREM sert de verrou atomique, donc
    une échéance n'est traitée que par UNE instance ;
  - la maintenance est protégée par un marqueur SET NX : une seule instance par intervalle.

Au démarrage : les files Redis et les échéances sont reconstruites depuis PostgreSQL
(Redis est un accélérateur, la base est la vérité).
"""
import asyncio
import logging
import time
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.redis import (
    DEADLINE_ABSENT,
    DEADLINE_WARNING,
    claim_due_deadlines,
    mark_once,
)
from app.services.ticket_service import TicketService

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]


class QueueScheduler:
    """Boucle de fond : traite les échéances toutes les SCHEDULER_INTERVAL_SECONDS secondes."""

    def __init__(self, session_factory: SessionFactory = AsyncSessionLocal) -> None:
        self._session_factory = session_factory
        self._task: asyncio.Task | None = None
        self._stopping = False
        self.last_run_at: float | None = None
        self.last_error: str | None = None

    # ── Cycle de vie ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Reconstruit l'état depuis la base, puis lance la boucle de fond."""
        await self.warm_up()
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="queue-scheduler")
        logger.info(
            f"Scheduler démarré (échéances toutes les {settings.SCHEDULER_INTERVAL_SECONDS}s, "
            f"absent après {settings.ABSENT_TIMEOUT_SECONDS}s)"
        )

    async def stop(self) -> None:
        """Arrête la boucle proprement."""
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Scheduler arrêté")

    @property
    def is_running(self) -> bool:
        """True si la boucle de fond tourne."""
        return self._task is not None and not self._task.done()

    async def warm_up(self) -> dict[str, int]:
        """
        Reconstruit les files Redis et les échéances depuis PostgreSQL.

        Ne lève jamais : un échec ici ne doit pas empêcher l'API de démarrer
        (l'auto-réparation par ticket prend le relais).
        """
        try:
            async with self._session_factory() as session:
                service = TicketService(session)
                queued = await service.rebuild_queue()
                deadlines = await service.rebuild_deadlines()
            logger.info(f"Warm-up : {queued} ticket(s) remis en file, {deadlines} échéance(s) reprogrammée(s)")
            return {"queued": queued, "deadlines": deadlines}
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Warm-up du Queue Engine échoué (non bloquant) : {exc}")
            return {"queued": 0, "deadlines": 0}

    # ── Boucle ────────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        """Exécute run_once() en continu ; une erreur n'arrête jamais la boucle."""
        while not self._stopping:
            try:
                await self.run_once()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                logger.error(f"Scheduler : erreur d'itération (la boucle continue) : {exc}", exc_info=True)
            await asyncio.sleep(settings.SCHEDULER_INTERVAL_SECONDS)

    async def run_once(self, now_ts: float | None = None) -> dict[str, int]:
        """
        Traite UNE itération : avertissements, absences automatiques, maintenance.

        Args:
            now_ts: Instant de référence (timestamp UNIX) — injectable pour les tests

        Returns:
            Compteurs : warnings, absent, expired, alerts
        """
        now_ts = now_ts if now_ts is not None else time.time()
        self.last_run_at = now_ts
        stats = {"warnings": 0, "absent": 0, "expired": 0, "alerts": 0}

        for org_id, ticket_id in await claim_due_deadlines(DEADLINE_WARNING, now_ts):
            if await self._safely(org_id, ticket_id, "avertissement", "send_absent_warning"):
                stats["warnings"] += 1

        for org_id, ticket_id in await claim_due_deadlines(DEADLINE_ABSENT, now_ts):
            if await self._safely(org_id, ticket_id, "absence automatique", "auto_mark_absent"):
                stats["absent"] += 1

        # Maintenance : une seule instance par intervalle (marqueur SET NX)
        if await mark_once("maintenance", max(1, settings.MAINTENANCE_INTERVAL_SECONDS - 1)):
            try:
                async with self._session_factory() as session:
                    service = TicketService(session)
                    expired = await service.expire_stale_tickets()
                    stats["expired"] = sum(expired.values())
                    stats["alerts"] = await service.alert_long_waits()
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Scheduler : maintenance échouée : {exc}", exc_info=True)

        return stats

    async def _safely(self, org_id: str, ticket_id: str, label: str, method: str) -> bool:
        """
        Applique une action planifiée sur un ticket dans sa propre session.

        Un ticket en erreur est journalisé puis ignoré : il ne doit jamais empêcher
        le traitement des suivants. Retourne True si l'action a eu un effet.
        """
        try:
            async with self._session_factory() as session:
                return bool(await getattr(TicketService(session), method)(org_id, ticket_id))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Scheduler : {label} échouée pour ticket={ticket_id} : {exc}")
            return False
