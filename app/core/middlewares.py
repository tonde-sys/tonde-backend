"""
Middlewares TONDE — Rate Limiting via slowapi.

Usage dans un router :
    from fastapi import Request
    from app.core.middlewares import limiter

    @router.post("/login")
    @limiter.limit("10/minute")
    async def login(request: Request, body: LoginEmailRequest, ...):
        ...

Note : request: Request doit être le PREMIER paramètre de chaque handler protégé.
C'est une contrainte de slowapi — il lit l'IP depuis l'objet Request.
"""
import logging

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from fastapi import FastAPI, WebSocket

logger = logging.getLogger(__name__)


# Instance globale partagée par tous les routers.
# key_func=get_remote_address → limite par adresse IP cliente.
# default_limits protège TOUTES les routes HTTP d'un plafond par IP dès que
# SlowAPIMiddleware est installé (voir setup_rate_limiting) — y compris les
# routes non décorées, qui sinon n'avaient aucune protection.
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200/minute"],
)


async def check_ws_rate_limit(websocket: WebSocket) -> bool:
    """
    Limite le nombre de connexions WebSocket par IP et par minute (DÉCISION 6).

    slowapi ne sait pas protéger un WebSocket : son décorateur exige un paramètre
    `request: Request` que les routes WebSocket n'ont pas, et plantait à CHAQUE
    connexion (« parameter `request` must be an instance of Request »). Le compteur
    est donc tenu dans Redis (INCR + EXPIRE), partagé entre toutes les instances.

    Si Redis est indisponible, la connexion est AUTORISÉE (fail-open) : une panne du
    limiteur ne doit pas couper le suivi en temps réel de tous les clients.

    Returns:
        True si la connexion est autorisée, False si la limite est dépassée.
    """
    from app.core.config import settings
    from app.core.redis import get_redis

    limit = settings.WS_CONNECT_LIMIT_PER_MINUTE
    if limit <= 0:
        return True  # 0 = limite désactivée
    host = websocket.client.host if websocket.client else "unknown"
    try:
        redis = await get_redis()
        key = f"tonde:ratelimit:ws:{host}"
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, 60)
        return count <= limit
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.warning(f"Limiteur WebSocket indisponible ({exc}) : connexion autorisée")
        return True


def setup_rate_limiting(app: FastAPI) -> None:
    """
    Attache le rate limiter à l'instance FastAPI.
    Doit être appelée dans main.py après la création de l'app,
    avant app.add_middleware() et app.include_router().

    Sans SlowAPIMiddleware, `default_limits` n'est JAMAIS appliqué : seules les
    routes portant un @limiter.limit explicite seraient protégées, et le plafond
    global annoncé (200/minute) resterait décoratif.

    Args:
        app: Instance FastAPI à protéger
    """
    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
