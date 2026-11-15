"""
HealthService — contrôles de santé pour l'exploitation (Kubernetes, Prometheus, ops).

  - liveness  : le processus répond (aucune dépendance testée)
  - readiness : la base ET Redis répondent (l'instance peut recevoir du trafic)
  - detailed  : latences, files, connexions WebSocket, scheduler, services externes

Les services externes (SMS, push) ne sont PAS appelés pendant un contrôle de santé :
cela coûterait de l'argent et ferait dépendre la santé de l'API d'un tiers.
On indique seulement s'ils sont configurés.

Un contrôle peut être **neutre** (« disabled », « not_wired ») : il est alors
rapporté dans la réponse mais ne fait pas échouer la sonde. Seul le statut
« unhealthy » dégrade la readiness.
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

# Statut qui, à lui seul, rend l'instance inapte à recevoir du trafic.
BLOCKING_STATUS = "unhealthy"

# Silence maximal toléré de la boucle de fond avant de la déclarer bloquée.
# La boucle tourne toutes les SCHEDULER_INTERVAL_SECONDS (5 s) : deux minutes de
# silence signifient qu'elle est morte ou bloquée sur un appel, pas qu'elle est
# simplement lente. Le plancher de 120 s évite les faux positifs quand
# l'intervalle est configuré très court.
SCHEDULER_STALE_AFTER_SECONDS = max(120, settings.SCHEDULER_INTERVAL_SECONDS * 6)


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


def check_scheduler(scheduler=None, now_ts: float | None = None) -> dict:
    """
    La boucle de fond tourne-t-elle encore ? (constat P2-18 de l'audit)

    Si elle meurt, plus aucun ticket `CALLED` ne passe en `ABSENT`, plus rien
    n'expire à la fermeture et la maintenance ne tourne plus — le tout sans
    qu'aucune sonde ne le voie. `/health/detailed` exposait déjà `last_run_at`,
    mais ce n'est pas l'URL interrogée par le monitoring : la panne était donc
    silencieuse jusqu'au premier client bloqué.

    Statuts retournés :
      - ``disabled``  : `SCHEDULER_ENABLED=false` — choix d'exploitation, neutre
      - ``not_wired`` : aucun scheduler fourni (tests, CLI) — neutre
      - ``starting``  : boucle lancée, première itération pas encore terminée
      - ``healthy``   : la boucle a tourné récemment
      - ``unhealthy`` : boucle arrêtée, bloquée, ou jamais aboutie — ``error``
        vaut alors ``loop_stopped``, ``stalled`` ou ``never_ran``

    Args:
        scheduler: QueueScheduler de l'application (``app.state.scheduler``)
        now_ts: Instant de référence injectable (tests)
    """
    if not settings.SCHEDULER_ENABLED:
        return {"status": "disabled", "last_run_age_seconds": None}

    if scheduler is None:
        return {"status": "not_wired", "last_run_age_seconds": None}

    now_ts = now_ts if now_ts is not None else time.time()

    # La boucle s'est arrêtée : `is_running` est faux alors qu'elle devrait tourner.
    if not scheduler.is_running:
        return {
            "status": "unhealthy",
            "error": "loop_stopped",
            "last_run_age_seconds": None if scheduler.last_run_at is None
            else round(now_ts - scheduler.last_run_at, 1),
            "last_error": scheduler.last_error,
        }

    # Boucle vivante mais aucune itération terminée : démarrage en cours. La
    # référence est alors `started_at` (positionné après le warm-up), sinon on
    # ne peut pas juger et on reste neutre.
    if scheduler.last_run_at is None:
        started_at = getattr(scheduler, "started_at", None)
        if started_at is None:
            return {"status": "starting", "last_run_age_seconds": None}
        age = round(now_ts - started_at, 1)
        if age > SCHEDULER_STALE_AFTER_SECONDS:
            return {
                "status": "unhealthy",
                "error": "never_ran",
                "last_run_age_seconds": age,
                "stale_after_seconds": SCHEDULER_STALE_AFTER_SECONDS,
                "last_error": scheduler.last_error,
            }
        return {"status": "starting", "last_run_age_seconds": age}

    age = round(now_ts - scheduler.last_run_at, 1)
    if age > SCHEDULER_STALE_AFTER_SECONDS:
        return {
            "status": "unhealthy",
            "error": "stalled",
            "last_run_age_seconds": age,
            "stale_after_seconds": SCHEDULER_STALE_AFTER_SECONDS,
            "last_error": scheduler.last_error,
        }

    return {
        "status": "healthy",
        "last_run_age_seconds": age,
        "stale_after_seconds": SCHEDULER_STALE_AFTER_SECONDS,
    }


async def readiness(
    session_factory: SessionFactory = AsyncSessionLocal,
    scheduler=None,
) -> tuple[bool, dict]:
    """
    Prêt à recevoir du trafic ?

    La base et Redis doivent répondre, et la boucle de fond doit être vivante
    quand elle est activée (constat P2-18). Les contrôles neutres (« disabled »,
    « not_wired », « starting ») n'empêchent pas la readiness.

    Args:
        session_factory: Fabrique de session SQLAlchemy
        scheduler: QueueScheduler de l'application, pour le contrôle de la boucle

    Returns:
        (ready, checks) — ready est False dès qu'un contrôle est « unhealthy ».
    """
    checks = {
        "database": await check_database(session_factory),
        "redis": await check_redis(),
        "scheduler": check_scheduler(scheduler),
    }
    return not any(c["status"] == BLOCKING_STATUS for c in checks.values()), checks


async def detailed_report(scheduler=None, session_factory: SessionFactory = AsyncSessionLocal) -> dict:
    """
    Rapport complet pour l'exploitation.

    Args:
        scheduler: QueueScheduler de l'application (état de la boucle de fond)
    """
    from app.websocket.queue_ws import ws_manager

    ready, checks = await readiness(session_factory, scheduler)
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
