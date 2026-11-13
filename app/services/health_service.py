"""
HealthService — contrôles de santé pour l'exploitation (Kubernetes, Prometheus, ops).

  - liveness  : le processus répond (aucune dépendance testée)
  - readiness : la base ET Redis répondent (l'instance peut recevoir du trafic)
  - detailed  : latences, files, connexions WebSocket, scheduler, services externes

Les services externes (SMS, push) ne sont PAS appelés pendant un contrôle de santé :
cela coûterait de l'argent et ferait dépendre la santé de l'API d'un tiers.
On indique seulement s'ils sont configurés.
"""
import time
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.metrics import request_metrics
from app.core.redis import count_all_queued_tickets, get_redis

SessionFactory = Callable[[], AsyncSession]


async def check_database(session_factory: SessionFactory = AsyncSessionLocal) -> dict:
    """Mesure la latence d'un SELECT 1. Ne lève jamais."""
    started = time.perf_counter()
    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "healthy", "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "unhealthy", "latency_ms": None, "error": type(exc).__name__}


async def check_redis() -> dict:
    """Mesure la latence d'un PING Redis. Ne lève jamais."""
    started = time.perf_counter()
    try:
        await (await get_redis()).ping()
        return {"status": "healthy", "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "unhealthy", "latency_ms": None, "error": type(exc).__name__}


def check_sms_gateway() -> dict:
    """État de configuration de la passerelle SMS (aucun appel réseau)."""
    if settings.is_development:
        return {"status": "simulated", "latency_ms": None}
    configured = bool(settings.AFRICAS_TALKING_API_KEY)
    return {"status": "configured" if configured else "not_configured", "latency_ms": None}


def check_push_gateway() -> dict:
    """État de configuration de Firebase Cloud Messaging (aucun appel réseau)."""
    if settings.is_development:
        return {"status": "simulated", "latency_ms": None}
    configured = bool(settings.FIREBASE_PROJECT_ID and settings.FIREBASE_SERVICE_ACCOUNT_JSON)
    return {"status": "configured" if configured else "not_configured", "latency_ms": None}


async def readiness(session_factory: SessionFactory = AsyncSessionLocal) -> tuple[bool, dict]:
    """
    Prêt à recevoir du trafic ?

    Returns:
        (ready, checks) — ready est True seulement si base ET Redis sont saines.
    """
    checks = {"database": await check_database(session_factory), "redis": await check_redis()}
    return all(c["status"] == "healthy" for c in checks.values()), checks


async def detailed_report(scheduler=None, session_factory: SessionFactory = AsyncSessionLocal) -> dict:
    """
    Rapport complet pour l'exploitation.

    Args:
        scheduler: QueueScheduler de l'application (état de la boucle de fond)
    """
    from app.websocket.queue_ws import ws_manager

    ready, checks = await readiness(session_factory)
    checks["sms_gateway"] = check_sms_gateway()
    checks["push_gateway"] = check_push_gateway()

    queue_size = None
    if checks["redis"]["status"] == "healthy":
        try:
            queue_size = await count_all_queued_tickets()
        except Exception:  # noqa: BLE001
            queue_size = None

    scheduler_state = None
    if scheduler is not None:
        age = None if scheduler.last_run_at is None else round(time.time() - scheduler.last_run_at, 1)
        scheduler_state = {
            "enabled": settings.SCHEDULER_ENABLED,
            "running": scheduler.is_running,
            "last_run_age_seconds": age,
            "last_error": scheduler.last_error,
        }

    return {
        "status": "healthy" if ready else "unhealthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
        "uptime_seconds": request_metrics.uptime_seconds(),
        "checks": checks,
        "metrics": {
            "active_connections": ws_manager.get_stats(),
            "requests_per_minute": request_metrics.requests_last_minute(),
            "requests_total": request_metrics.total_requests,
            "responses_by_class": dict(request_metrics.by_status_class),
            "queue_size": queue_size,
        },
        "scheduler": scheduler_state,
    }
