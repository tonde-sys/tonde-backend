"""
Middlewares TONDE — Rate Limiting via slowapi + limitation des connexions WebSocket.

Deux mécanismes distincts, pour deux besoins distincts :

1. HTTP — slowapi, appliqué par SlowAPIMiddleware.
   `default_limits` protège TOUTES les routes d'un plafond global par IP.
   Les routes sensibles (login, OTP) ajoutent une limite plus stricte :

        from fastapi import Request
        from app.core.middlewares import limiter

        @router.post("/login")
        @limiter.limit("10/minute")
        async def login(request: Request, body: LoginEmailRequest, ...):
            ...

   Note : request: Request doit être le PREMIER paramètre de chaque handler
   protégé. C'est une contrainte de slowapi — il lit l'IP depuis l'objet Request.

2. WebSocket — slowapi NE SUPPORTE PAS les WebSockets : son décorateur exige un
   objet `Request` et lève une exception sur une route WS. On utilise donc
   check_ws_connection_limit() : un compteur Redis à fenêtre glissante, appelé
   explicitement en tête de chaque handler WebSocket.
"""
import logging

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from fastapi import FastAPI, WebSocket

logger = logging.getLogger(__name__)


# ── Limite HTTP ───────────────────────────────────────────────────────────────
# Instance globale UNIQUE, partagée par main.py et tous les routers.
# default_limits s'applique à toute requête HTTP dès que SlowAPIMiddleware
# est installé (voir setup_rate_limiting) — y compris aux routes non décorées.
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200/minute"],
)


def setup_rate_limiting(app: FastAPI) -> None:
    """
    Attache le rate limiter à l'instance FastAPI.

    Doit être appelée dans main.py après la création de l'app et AVANT
    app.include_router(), mais AVANT CORS pour que CORS reste le middleware
    le plus externe (les réponses 429 doivent porter les en-têtes CORS).

    Sans SlowAPIMiddleware, default_limits n'est JAMAIS appliqué : seules les
    routes portant @limiter.limit seraient protégées.

    Args:
        app: Instance FastAPI à protéger
    """
    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ── Limite WebSocket ──────────────────────────────────────────────────────────
WS_CONNECT_LIMIT = 10          # Connexions autorisées…
WS_CONNECT_WINDOW = 60         # …par fenêtre de 60 secondes, par IP
WS_CLOSE_RATE_LIMITED = 4029   # Code de fermeture applicatif (plage 4000-4999)


async def check_ws_connection_limit(
    websocket: WebSocket,
    limit: int = WS_CONNECT_LIMIT,
    window_seconds: int = WS_CONNECT_WINDOW,
) -> bool:
    """
    Vérifie qu'une IP n'ouvre pas trop de WebSockets sur la fenêtre courante.

    Compteur Redis à TTL : la clé expire d'elle-même, aucune tâche de nettoyage
    n'est nécessaire. En cas d'indisponibilité Redis, on laisse passer la
    connexion (fail-open) : mieux vaut un temps réel dégradé qu'un service
    entièrement bloqué par son cache.

    Args:
        websocket: La connexion entrante (IP lue depuis websocket.client)
        limit: Nombre maximal de connexions sur la fenêtre
        window_seconds: Durée de la fenêtre en secondes

    Returns:
        True si la connexion est autorisée, False si la limite est atteinte.
    """
    try:
        from app.core.redis import get_redis

        client_ip = websocket.client.host if websocket.client else "unknown"
        key = f"tonde:ws_connect:{client_ip}"

        r = await get_redis()
        count = await r.incr(key)
        if count == 1:
            # Première connexion de la fenêtre → démarrer le TTL
            await r.expire(key, window_seconds)

        if count > limit:
            logger.warning(
                f"[SECURITY] Limite WebSocket atteinte pour {client_ip} "
                f"({count}/{limit} sur {window_seconds}s)"
            )
            return False
        return True

    except Exception as e:
        logger.warning(f"Limite WebSocket indisponible (fail-open): {e}")
        return True
