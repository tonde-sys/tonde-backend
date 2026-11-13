"""
Tests de régression — rate limiting HTTP et contrat des routes WebSocket.

Deux bugs corrigés ici :

1. `default_limits=["200/minute"]` était déclaré mais jamais appliqué, faute de
   SlowAPIMiddleware dans la pile. Le plafond global annoncé était purement
   décoratif : seules les routes portant un décorateur explicite étaient
   protégées.

2. `@limiter.limit(...)` avait été appliqué à des routes WebSocket. slowapi
   exige un objet `Request` : chaque connexion WS levait
   "parameter `request` must be an instance of starlette.requests.Request".
   Le temps réel — cœur du produit — était donc totalement cassé.

Le comportement du compteur WebSocket (allow / deny / fail-open) est couvert
par tests/test_rate_limiting.py ; on vérifie ici le CONTRAT structurel.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import Limiter
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

import pytest


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
