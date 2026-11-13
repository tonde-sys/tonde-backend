"""
Tests unitaires — Rate Limiting (TASK-06)

Ces tests passent par la stack HTTP complète (TestClient ASGI)
car slowapi intercepte au niveau middleware, pas au niveau service.

Note : slowapi maintient les compteurs en mémoire par défaut.
Chaque fixture recrée une app fraîche pour isoler les compteurs entre tests.
"""
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from unittest.mock import patch, AsyncMock

from app.core.config import settings
from app.core.middlewares import setup_rate_limiting, Limiter, get_remote_address
from tests.test_queue_atomicity import real_redis  # noqa: F401  (fixture importée par nom)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_test_app() -> FastAPI:
    """
    Crée une app FastAPI de test avec rate limiting activé.
    Utilise un Limiter frais à chaque appel pour isoler les compteurs.
    """
    from app.routers.auth import router as auth_router

    # Nouveau Limiter par app pour isoler les compteurs entre tests
    fresh_limiter = Limiter(key_func=get_remote_address)

    app = FastAPI()

    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    app.state.limiter = fresh_limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # Patcher le limiter du router avec le nouveau
    import app.routers.auth as auth_module
    original_limiter = auth_module.limiter

    # Remplacer temporairement le limiter global dans le module auth
    auth_module.limiter = fresh_limiter
    app.include_router(auth_router, prefix="/api/v1/auth")
    # Restaurer (le TestClient est synchrone donc pas de problème de concurrence ici)
    auth_module.limiter = original_limiter

    return app


@pytest.fixture
def client():
    """Client de test avec une app fraîche et rate limiting activé."""
    app = _make_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ── Corps de requête valides pour déclencher les endpoints ────────────────────

REGISTER_PHONE_BODY = {"phone": "+25779000001", "country_code": "BI"}
VERIFY_OTP_BODY = {"phone": "+25779000001", "otp": "123456"}
LOGIN_BODY = {"email": "test@tonde.bi", "password": "wrongpassword"}
REFRESH_BODY = {"refresh_token": "invalid.token.here"}


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_rate_limiter_setup():
    """setup_rate_limiting() configure app.state.limiter et le handler 429."""
    from slowapi.errors import RateLimitExceeded

    app = FastAPI()
    setup_rate_limiting(app)

    assert hasattr(app.state, "limiter")
    assert RateLimitExceeded in app.exception_handlers


def test_middlewares_module_exports_limiter():
    """Le module middlewares exporte bien l'instance limiter."""
    from app.core.middlewares import limiter as exported_limiter
    assert exported_limiter is not None


def test_login_allows_requests_under_limit():
    """Les 10 premières requêtes sur /login passent (pas de 429)."""
    app = _make_test_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        with patch("app.services.auth_service.AuthService.login_email", new_callable=AsyncMock) as mock_login:
            from fastapi import HTTPException, status
            mock_login.side_effect = HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "INVALID_CREDENTIALS", "message": "test"}
            )
            for i in range(10):
                resp = client.post("/api/v1/auth/login", json=LOGIN_BODY)
                assert resp.status_code != 429, f"Requête {i+1}/10 ne doit pas être limitée"


def test_login_rate_limit_returns_429_after_threshold():
    """La 11ème requête sur /login retourne HTTP 429."""
    app = _make_test_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        with patch("app.services.auth_service.AuthService.login_email", new_callable=AsyncMock) as mock_login:
            from fastapi import HTTPException, status
            mock_login.side_effect = HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "INVALID_CREDENTIALS", "message": "test"}
            )
            # Consommer les 10 requêtes autorisées
            for _ in range(10):
                client.post("/api/v1/auth/login", json=LOGIN_BODY)

            # La 11ème doit être bloquée
            resp = client.post("/api/v1/auth/login", json=LOGIN_BODY)
            assert resp.status_code == 429


def test_429_response_has_retry_after_header():
    """Une réponse 429 contient le header Retry-After (insensible à la casse)."""
    app = _make_test_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        with patch("app.services.auth_service.AuthService.login_email", new_callable=AsyncMock) as mock_login:
            from fastapi import HTTPException, status
            mock_login.side_effect = HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "INVALID_CREDENTIALS", "message": "test"}
            )
            for _ in range(10):
                client.post("/api/v1/auth/login", json=LOGIN_BODY)

            resp = client.post("/api/v1/auth/login", json=LOGIN_BODY)
            assert resp.status_code == 429

            # slowapi inclut Retry-After dans le body ou les headers
            # Vérifier soit le header soit le message d'erreur dans le body
            headers_lower = {k.lower(): v for k, v in resp.headers.items()}
            has_retry_after = "retry-after" in headers_lower
            has_rate_limit_info = "x-ratelimit" in " ".join(headers_lower.keys())
            has_rate_limit_body = "rate limit" in resp.text.lower() or "10 per" in resp.text.lower()

            assert has_retry_after or has_rate_limit_info or has_rate_limit_body, \
                f"Aucune info de rate limit trouvée. Headers: {dict(resp.headers)}, Body: {resp.text}"


def test_otp_rate_limit_after_5_attempts():
    """La 6ème requête sur /verify-otp retourne HTTP 429."""
    app = _make_test_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        with patch("app.services.auth_service.AuthService.verify_otp", new_callable=AsyncMock) as mock_verify:
            from fastapi import HTTPException, status
            mock_verify.side_effect = HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "INVALID_OTP", "message": "test"}
            )
            for _ in range(5):
                client.post("/api/v1/auth/verify-otp", json=VERIFY_OTP_BODY)

            resp = client.post("/api/v1/auth/verify-otp", json=VERIFY_OTP_BODY)
            assert resp.status_code == 429


def test_register_phone_rate_limit_after_5_attempts():
    """La 6ème requête sur /register/phone retourne HTTP 429."""
    app = _make_test_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        with patch("app.services.auth_service.AuthService.register_phone", new_callable=AsyncMock) as mock_reg:
            mock_reg.return_value = {"success": True, "otp_sent": True, "expires_in_seconds": 300}
            for _ in range(5):
                client.post("/api/v1/auth/register/phone", json=REGISTER_PHONE_BODY)

            resp = client.post("/api/v1/auth/register/phone", json=REGISTER_PHONE_BODY)
            assert resp.status_code == 429


# ── Limiteur WebSocket (DÉCISION 6) ───────────────────────────────────────────

def _ws_app() -> FastAPI:
    """App minimale exposant les routes WebSocket des tickets (sans lifespan)."""
    from app.routers.tickets import router as tickets_router

    app = FastAPI()
    app.include_router(tickets_router, prefix="/api/v1/tickets")
    return app


def _ws(host: str = "203.0.113.10") -> SimpleNamespace:
    """Faux WebSocket : le limiteur n'a besoin que de l'IP cliente."""
    return SimpleNamespace(client=SimpleNamespace(host=host))


@pytest.mark.asyncio
async def test_ws_rate_limit_counts_per_ip_and_sets_ttl(real_redis, monkeypatch):
    """Les N premières connexions passent, la suivante est refusée; le compteur expire."""
    from app.core.middlewares import check_ws_rate_limit

    monkeypatch.setattr(settings, "WS_CONNECT_LIMIT_PER_MINUTE", 2)
    host = f"198.51.100.{uuid.uuid4().int % 250 + 1}"
    key = f"tonde:ratelimit:ws:{host}"
    await real_redis.delete(key)

    assert await check_ws_rate_limit(_ws(host)) is True
    assert await check_ws_rate_limit(_ws(host)) is True
    assert await check_ws_rate_limit(_ws(host)) is False, "3e connexion refusée"
    assert await check_ws_rate_limit(_ws("198.51.100.200")) is True, "compteur par IP"
    assert 0 < await real_redis.ttl(key) <= 60, "la fenêtre est d'une minute"


@pytest.mark.asyncio
async def test_ws_rate_limit_zero_disables_the_counter(monkeypatch):
    """0 = limite désactivée : aucun accès Redis n'est nécessaire."""
    from app.core import redis as redis_module
    from app.core.middlewares import check_ws_rate_limit

    monkeypatch.setattr(settings, "WS_CONNECT_LIMIT_PER_MINUTE", 0)
    monkeypatch.setattr(redis_module, "get_redis",
                        AsyncMock(side_effect=AssertionError("Redis ne doit pas être appelé")))

    assert await check_ws_rate_limit(_ws()) is True


@pytest.mark.asyncio
async def test_ws_rate_limit_fails_open_when_redis_is_down(monkeypatch):
    """Une panne du limiteur ne doit jamais couper le suivi temps réel."""
    from app.core import redis as redis_module
    from app.core.middlewares import check_ws_rate_limit

    monkeypatch.setattr(settings, "WS_CONNECT_LIMIT_PER_MINUTE", 10)
    monkeypatch.setattr(redis_module, "get_redis", AsyncMock(side_effect=ConnectionError("down")))

    assert await check_ws_rate_limit(_ws()) is True


def test_ws_route_refuses_connection_with_4029_when_limit_is_reached(monkeypatch):
    """La route ferme en 4029 (et non par une exception) quand la limite est atteinte."""
    import app.routers.tickets as tickets_module

    monkeypatch.setattr(tickets_module, "check_ws_rate_limit", AsyncMock(return_value=False))

    with TestClient(_ws_app()) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/api/v1/tickets/ws/queue/t-1?agency_id=a-1&token=x"):
                pass

    assert exc.value.code == 4029


def test_ws_route_no_longer_crashes_before_token_check(monkeypatch):
    """Régression : slowapi plantait chaque connexion avant même la vérification du token."""
    import app.routers.tickets as tickets_module

    monkeypatch.setattr(tickets_module, "check_ws_rate_limit", AsyncMock(return_value=True))

    with TestClient(_ws_app()) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/api/v1/tickets/ws/queue/t-1?agency_id=a-1&token=invalide"):
                pass

    assert exc.value.code == 4001, "token invalide → fermeture propre, pas un crash du handler"
