"""
Tests de régression — Contrat WebSocket & rate limiting.

Deux bugs corrigés ici :

1. `@limiter.limit(...)` était appliqué à des routes WebSocket. slowapi exige
   un objet `Request` : chaque connexion WS levait
   "parameter `request` must be an instance of starlette.requests.Request".
   Le temps réel — cœur du produit — était donc totalement cassé.

2. `default_limits=["200/minute"]` était déclaré mais jamais appliqué, faute de
   SlowAPIMiddleware. Le plafond global était purement décoratif.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import Limiter
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address


# ── 1. Rate limiting HTTP global ─────────────────────────────────────────────

def test_default_limits_are_enforced(monkeypatch):
    """
    setup_rate_limiting() doit rendre default_limits effectif.

    Avant correctif : 5 requêtes sur une limite de 3/min → 5× 200.
    """
    from app.core import middlewares

    fresh = Limiter(key_func=get_remote_address, default_limits=["3/minute"])
    monkeypatch.setattr(middlewares, "limiter", fresh)

    app = FastAPI()
    middlewares.setup_rate_limiting(app)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    client = TestClient(app)
    codes = [client.get("/ping").status_code for _ in range(5)]

    assert codes.count(429) >= 1, f"default_limits non appliqué : {codes}"


def test_setup_rate_limiting_installs_middleware():
    """Le middleware slowapi est bien dans la pile."""
    from app.core import middlewares

    app = FastAPI()
    middlewares.setup_rate_limiting(app)

    assert app.state.limiter is middlewares.limiter
    assert any(m.cls is SlowAPIMiddleware for m in app.user_middleware)


# ── 2. Contrat des routes WebSocket ──────────────────────────────────────────

def _get_app():
    """Importe l'app réelle, ou skip si l'environnement ne le permet pas."""
    try:
        from app.main import app
        return app
    except Exception as e:  # pragma: no cover - dépend de l'environnement local
        pytest.skip(f"app.main non importable sans configuration: {e}")


def test_websocket_routes_are_not_wrapped_by_slowapi():
    """
    Aucune route WebSocket ne doit porter de décorateur slowapi.

    Détection : `@limiter.limit` enveloppe le handler avec functools.wraps,
    ce qui expose un attribut `__wrapped__`. Un handler WS natif n'en a pas.
    """
    from starlette.routing import WebSocketRoute

    app = _get_app()
    offenders = []

    for route in app.routes:
        if isinstance(route, WebSocketRoute) and hasattr(route.endpoint, "__wrapped__"):
            offenders.append(route.path)

    assert offenders == [], (
        f"Ces routes WebSocket sont enveloppées par slowapi et lèveront "
        f"une exception à chaque connexion : {offenders}"
    )


def test_ws_queue_connection_no_longer_crashes(monkeypatch):
    """
    Le handler WebSocket doit aboutir au message 'connected'.

    Avant correctif, l'appel levait l'exception slowapi
    "parameter `request` must be an instance of starlette.requests.Request".

    On appelle le handler directement (avec un faux WebSocket) plutôt que via
    TestClient : cela isole le contrat du handler sans démarrer la lifespan
    (qui exigerait PostgreSQL et Redis réels).
    """
    import asyncio

    from fastapi import WebSocketDisconnect

    from app.core.security import create_access_token
    from app.routers.tickets import websocket_queue

    # Redis mocké : la limite de connexion passe
    redis_mock = AsyncMock()
    redis_mock.incr = AsyncMock(return_value=1)
    redis_mock.expire = AsyncMock(return_value=True)
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=redis_mock))

    user_id = "user-ws-contract"
    ticket = MagicMock()
    ticket.id = "ticket-ws-1"
    ticket.user_id = user_id
    ticket.agency_id = "branch-ws-1"
    ticket.org_id = "org-ws-1"

    class _FakeResult:
        def scalar_one_or_none(self):
            return ticket

    class _FakeSession:
        async def execute(self, *args, **kwargs):
            return _FakeResult()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def commit(self):
            return None

        async def close(self):
            return None

    class _FakeWebSocket:
        """WebSocket minimal : accepte, capture le message, puis simule la fermeture."""

        def __init__(self):
            self.sent: list[str] = []
            self.accepted = False
            self.close_code: int | None = None
            self.client = MagicMock()
            self.client.host = "41.0.0.10"

        async def accept(self):
            self.accepted = True

        async def send_text(self, text: str):
            self.sent.append(text)

        async def receive_text(self):
            raise WebSocketDisconnect(code=1000)

        async def close(self, code: int = 1000, reason: str = ""):
            self.close_code = code

    fake_ws = _FakeWebSocket()
    token = create_access_token(user_id)

    # Aucune exception ne doit s'échapper (c'était le bug)
    asyncio.run(
        websocket_queue(
            websocket=fake_ws,
            ticket_id="ticket-ws-1",
            agency_id="branch-ws-1",
            token=token,
            db=_FakeSession(),
        )
    )

    assert fake_ws.accepted is True
    assert any("connected" in message for message in fake_ws.sent)
    assert fake_ws.close_code is None  # fermeture propre, pas un refus


# ── 3. Limite de connexions WebSocket ────────────────────────────────────────

@pytest.mark.asyncio
async def test_ws_limit_allows_under_threshold(monkeypatch):
    """Sous le seuil, la connexion est autorisée."""
    from app.core import middlewares

    redis_mock = AsyncMock()
    redis_mock.incr = AsyncMock(return_value=3)
    redis_mock.expire = AsyncMock(return_value=True)
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=redis_mock))

    websocket = MagicMock()
    websocket.client.host = "41.0.0.1"

    assert await middlewares.check_ws_connection_limit(websocket, limit=10) is True
    redis_mock.expire.assert_not_awaited()  # TTL posé seulement au 1er incrément


@pytest.mark.asyncio
async def test_ws_limit_starts_ttl_on_first_connection(monkeypatch):
    """Le TTL est posé au premier incrément, pour que la clé expire seule."""
    from app.core import middlewares

    redis_mock = AsyncMock()
    redis_mock.incr = AsyncMock(return_value=1)
    redis_mock.expire = AsyncMock(return_value=True)
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=redis_mock))

    websocket = MagicMock()
    websocket.client.host = "41.0.0.2"

    assert await middlewares.check_ws_connection_limit(websocket) is True
    redis_mock.expire.assert_awaited_once()


@pytest.mark.asyncio
async def test_ws_limit_denies_above_threshold(monkeypatch):
    """Au-delà du seuil, la connexion est refusée."""
    from app.core import middlewares

    redis_mock = AsyncMock()
    redis_mock.incr = AsyncMock(return_value=11)
    redis_mock.expire = AsyncMock(return_value=True)
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=redis_mock))

    websocket = MagicMock()
    websocket.client.host = "41.0.0.3"

    assert await middlewares.check_ws_connection_limit(websocket, limit=10) is False


@pytest.mark.asyncio
async def test_ws_limit_fails_open_when_redis_down(monkeypatch):
    """Redis indisponible → on laisse passer (mieux qu'un temps réel bloqué)."""
    from app.core import middlewares

    broken = AsyncMock()
    broken.incr = AsyncMock(side_effect=ConnectionError("redis down"))
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=broken))

    websocket = MagicMock()
    websocket.client.host = "41.0.0.4"

    assert await middlewares.check_ws_connection_limit(websocket) is True
