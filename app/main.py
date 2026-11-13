"""
TONDE BACKEND — Point d'entrée principal
========================================
Configure FastAPI, initialise les connexions et branche les routers.

En production : les migrations sont gérées par Alembic.
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
from app.core.middlewares import setup_rate_limiting
from app.core.redis import get_redis, close_redis

# Import explicite des modèles — obligatoire pour que Base.metadata
# connaisse toutes les tables avant toute opération DB
import app.models  # noqa: F401

from app.routers import auth, tickets, organizations, branches, counters, employees, analytics
from app.routers import services as services_router
from app.websocket.queue_ws import ws_manager

logger = logging.getLogger(__name__)


# ── Démarrage et arrêt de l'app ───────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Code exécuté au démarrage et à l'arrêt de l'API.
    Initialise les connexions DB et Redis.

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

    # Démarrer le listener Redis Pub/Sub en tâche asyncio de fond
    # Écoute tonde:events:* — couvre toutes les orgs — se reconnecte auto en cas d'erreur
    asyncio.create_task(ws_manager.start_redis_listener())
    logger.info("Redis Pub/Sub — Listener démarré en tâche de fond")

    logger.info(f"API prête sur http://localhost:{settings.APP_PORT}")
    logger.info(f"Documentation : http://localhost:{settings.APP_PORT}/docs")

    yield  # L'app tourne ici

    # Arrêt propre
    await close_redis()
    logger.info("TONDE API — Arrêt propre")


# ── Création de l'app FastAPI ─────────────────────────────────────────────────
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
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ── Rate limiting — instance unique + SlowAPIMiddleware ──────────────────────
# setup_rate_limiting() installe le middleware qui rend default_limits effectif
# (200/minute par IP sur toutes les routes HTTP) et attache app.state.limiter.
# Doit précéder add_middleware(CORS) pour que CORS reste le plus externe.
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


# ── Routes ────────────────────────────────────────────────────────────────────
app.include_router(auth.router,            prefix="/api/v1/auth",          tags=["🔐 Auth"])
app.include_router(tickets.router,         prefix="/api/v1/tickets",       tags=["🎫 Tickets"])
app.include_router(organizations.router,   prefix="/api/v1/organizations", tags=["🏢 Organizations"])
app.include_router(branches.router,        prefix="/api/v1/organizations", tags=["🏦 Branches"])
app.include_router(services_router.router, prefix="/api/v1",               tags=["🛎 Services"])
app.include_router(analytics.router,       prefix="/api/v1/organizations", tags=["📊 Analytics"])
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
        "docs": "/docs",
        "environment": settings.ENVIRONMENT,
    }


@app.get("/health", tags=["🏠 Accueil"])
async def health_check():
    """
    Endpoint de santé — vérifié par Docker, load balancers et monitoring.
    Teste Redis et retourne un statut dégradé si un service est KO.
    """
    from sqlalchemy import text
    from app.core.database import AsyncSessionLocal

    # Test Redis
    redis_ok = False
    try:
        redis = await get_redis()
        await redis.ping()
        redis_ok = True
    except Exception as e:
        logger.warning(f"Health check Redis KO: {e}")

    # Test PostgreSQL
    db_ok = False
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
            db_ok = True
    except Exception as e:
        logger.warning(f"Health check DB KO: {e}")

    all_ok = redis_ok and db_ok

    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={
            "status": "ok" if all_ok else "degraded",
            "version": settings.APP_VERSION,
            "environment": settings.ENVIRONMENT,
            "services": {
                "api": "ok",
                "redis": "ok" if redis_ok else "error",
                "database": "ok" if db_ok else "error",
            },
        },
    )


# ── Gestion globale des erreurs ───────────────────────────────────────────────
@app.exception_handler(404)
async def not_found_handler(request: Request, exc) -> JSONResponse:
    """
    Uniformise les 404 au format d'erreur TONDE.

    Les 404 MÉTIER (TICKET_NOT_FOUND, BRANCH_NOT_FOUND…) portent déjà un code :
    ils sont renvoyés tels quels. Avant, ce handler les écrasait tous par
    « cette route n'existe pas », ce qui rendait le débogage mobile impossible.
    """
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict) and "code" in detail:
        return JSONResponse(
            status_code=404,
            content={"success": False, "error": detail},
        )

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
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Capture les exceptions NON gérées.

    @app.exception_handler(500) ne se déclenche que pour une HTTPException(500)
    explicite : une vraie erreur inattendue (panne DB, KeyError, timeout Redis)
    renvoyait la page HTML par défaut de Starlette, hors du contrat JSON de
    l'API — le mobile recevait alors un corps illisible.
    """
    logger.error(f"Erreur non gérée sur {request.url.path}: {exc}", exc_info=exc)
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
