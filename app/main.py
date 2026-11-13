"""
TONDE BACKEND — Point d'entrée principal
========================================
Configure FastAPI, initialise les connexions et branche les routers.

En production : les migrations sont gérées par Alembic, la configuration est
auditée au démarrage (voir app/core/config.py) et la documentation interactive est désactivée.
En développement : create_tables() peut créer les tables au démarrage
                   si CREATE_TABLES_ON_STARTUP=true dans .env.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from app.core.config import settings
from app.core.metrics import request_metrics
from app.core.middlewares import setup_rate_limiting
from app.core.redis import close_redis, get_redis

# Import explicite des modèles — obligatoire pour que Base.metadata
# connaisse toutes les tables avant toute opération DB
import app.models  # noqa: F401

from app.routers import (
    analytics,
    auth,
    branches,
    counters,
    employees,
    exports,
    health,
    notifications,
    organizations,
    public,
    sync,
    tickets,
)
from app.routers import services as services_router
from app.services.queue_scheduler import QueueScheduler
from app.websocket.queue_ws import ws_manager

logger = logging.getLogger(__name__)


# ── Démarrage et arrêt de l'app ───────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Code exécuté au démarrage et à l'arrêt de l'API.

    Démarrage : tables (dev), Redis, listener Pub/Sub, puis Queue Scheduler
    (reconstruction des files depuis PostgreSQL + auto-absent + maintenance).

    En production, NE PAS utiliser create_tables().
    Utiliser : docker-compose exec api alembic upgrade head
    """
    logger.info("=" * 50)
    logger.info("TONDE API — Démarrage")
    logger.info(f"Environnement : {settings.ENVIRONMENT}")
    logger.info(f"Version       : {settings.APP_VERSION}")
    logger.info("=" * 50)

    # create_tables() uniquement en développement si flag activé
    if settings.CREATE_TABLES_ON_STARTUP:
        from app.core.database import create_tables
        await create_tables()
        logger.info("Base de données — Tables créées (mode dev)")
    else:
        logger.info("Base de données — Utiliser 'alembic upgrade head' pour les migrations")

    # Vérifier la connexion Redis
    redis = await get_redis()
    await redis.ping()
    logger.info("Redis — Connexion établie")

    # Listener Redis Pub/Sub en tâche asyncio de fond
    # Écoute tonde:events:* — couvre toutes les orgs — se reconnecte auto en cas d'erreur
    listener_task = asyncio.create_task(ws_manager.start_redis_listener())
    logger.info("Redis Pub/Sub — Listener démarré en tâche de fond")

    # Queue Scheduler : warm-up (files + échéances depuis PostgreSQL), auto-absent, maintenance
    scheduler = QueueScheduler()
    app.state.scheduler = scheduler
    if settings.SCHEDULER_ENABLED:
        await scheduler.start()
    else:
        logger.warning("Scheduler désactivé (SCHEDULER_ENABLED=false) : pas d'auto-absent ni de maintenance")

    logger.info(f"API prête sur http://localhost:{settings.APP_PORT}")
    if not settings.is_production:
        logger.info(f"Documentation : http://localhost:{settings.APP_PORT}/docs")

    yield  # L'app tourne ici

    # Arrêt propre
    await scheduler.stop()
    listener_task.cancel()
    try:
        await listener_task
    except asyncio.CancelledError:
        pass
    await close_redis()
    logger.info("TONDE API — Arrêt propre")


# ── Création de l'app FastAPI ─────────────────────────────────────────────────
# En production, la documentation interactive n'est PAS exposée : elle décrit
# précisément toute la surface d'attaque de l'API.
app = FastAPI(
    title="Tonde API",
    description="""
## Tonde — Smart Queue Management System

API REST + WebSocket pour la gestion intelligente de file d'attente.

### Authentification
Utilisez le bouton **Authorize** ci-dessus et entrez : `Bearer <votre_token>`

### Environnement actuel
L'OTP de test en développement est toujours **123456**
    """,
    version=settings.APP_VERSION,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
    lifespan=lifespan,
)

# ── Rate limiting — UNE seule instance partagée avec les décorateurs des routers ──
# Les limites sont définies route par route (@limiter.limit) : login, OTP, refresh,
# WebSocket, QR public, sync… Avant, main.py créait son propre Limiter (jamais
# utilisé par les décorateurs), donc la limite « globale » annoncée n'existait pas.
setup_rate_limiting(app)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Retourne une erreur 429 structurée quand la limite IP est dépassée."""
    return JSONResponse(
        status_code=429,
        content={
            "success": False,
            "error": {
                "code": "RATE_LIMIT_EXCEEDED",
                "message": (
                    "Trop de requêtes depuis votre adresse. "
                    "Veuillez patienter avant de réessayer."
                ),
                "retry_after": getattr(exc, "retry_after", 60),
            },
        },
        headers={"Retry-After": str(getattr(exc, "retry_after", 60))},
    )


# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Métriques de requêtes (alimentent /health/detailed) ───────────────────────
@app.middleware("http")
async def record_request_metrics(request: Request, call_next):
    """Compte chaque requête HTTP et son code de réponse (hors WebSocket)."""
    try:
        response = await call_next(request)
    except Exception:
        request_metrics.record(500)
        raise
    request_metrics.record(response.status_code)
    return response


# ── Routes ────────────────────────────────────────────────────────────────────
app.include_router(health.router)  # /health, /health/live, /health/ready, /health/detailed
app.include_router(auth.router,            prefix="/api/v1/auth",          tags=["🔐 Auth"])
app.include_router(tickets.router,         prefix="/api/v1/tickets",       tags=["🎫 Tickets"])
app.include_router(sync.router,            prefix="/api/v1/sync",          tags=["🔄 Sync hors-ligne"])
app.include_router(notifications.router,   prefix="/api/v1/notifications", tags=["🔔 Notifications"])
app.include_router(organizations.router,   prefix="/api/v1/organizations", tags=["🏢 Organizations"])
app.include_router(branches.router,        prefix="/api/v1/organizations", tags=["🏦 Branches"])
app.include_router(services_router.router, prefix="/api/v1",               tags=["🛎 Services"])
app.include_router(public.router,          prefix="/api/v1",               tags=["📺 Public"])
app.include_router(analytics.router,       prefix="/api/v1/organizations", tags=["📊 Analytics"])
app.include_router(exports.router,         prefix="/api/v1/organizations", tags=["📤 Exports"])
app.include_router(
    counters.router,
    prefix="/api/v1/organizations/{org_id}/branches/{branch_id}/counters",
    tags=["🏧 Counters"],
)
app.include_router(
    employees.router,
    prefix="/api/v1/organizations/{org_id}/employees",
    tags=["👥 Employees"],
)


# ── Endpoints de base ─────────────────────────────────────────────────────────
@app.get("/", tags=["🏠 Accueil"])
async def root():
    return {
        "app": "Tonde API",
        "version": settings.APP_VERSION,
        "status": "running",
        "docs": None if settings.is_production else "/docs",
        "environment": settings.ENVIRONMENT,
    }


# ── Gestion globale des erreurs ───────────────────────────────────────────────
@app.exception_handler(404)
async def not_found_handler(request, exc):
    # Les 404 métier lèvent HTTPException(detail={code, message}) : on les laisse passer.
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict) and "code" in detail:
        return JSONResponse(status_code=404, content={"detail": detail})
    return JSONResponse(
        status_code=404,
        content={
            "success": False,
            "error": {
                "code": "NOT_FOUND",
                "message": f"La route {request.url.path} n'existe pas",
            },
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc: Exception):
    """
    Filet de sécurité pour TOUTE exception non gérée.

    On enregistre Exception et non 500 : Starlette n'invoque jamais un handler
    déclaré sur la classe 500 (aucune exception de ce type n'est levée). Le
    handler `500` précédent était donc du code mort, et les vraies erreurs
    sortaient en texte brut « Internal Server Error » — sans enveloppe JSON,
    sans log applicatif, et sans en-têtes CORS côté client.

    Les HTTPException (4xx/5xx métier) ne passent PAS ici : Starlette les
    traite avant avec leur propre handler.
    """
    logger.exception(f"Erreur non gérée sur {request.method} {request.url.path}")
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": {
                "code": "SERVER_ERROR",
                "message": "Erreur interne du serveur. Contactez le support Tonde.",
            },
        },
    )
