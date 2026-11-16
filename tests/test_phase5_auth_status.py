"""
401 vs 403 — un jeton ABSENT doit renvoyer 401, pas 403.

Le défaut d'origine venait de `HTTPBearer()` : avec `auto_error=True` (la valeur
par défaut), FastAPI lève lui-même un 403 quand l'en-tête `Authorization` est
absent. Le client ne peut alors pas distinguer :

  401 → « je ne sais pas qui tu es »      → se connecter (ou rafraîchir le jeton)
  403 → « je sais qui tu es, mais non »   → se connecter ne changera rien

Un mobile qui reçoit 403 sur un jeton expiré ou absent ne déclenche pas son
écran de connexion : il affiche une erreur d'autorisation et l'utilisateur reste
bloqué. C'est la raison pour laquelle la RFC 7235 impose 401 + `WWW-Authenticate`
quand l'authentification manque.

Les deux niveaux sont testés : la dépendance seule (rapide, sans Redis) et le
comportement HTTP réel de bout en bout.
"""
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt

from app.core.config import settings
from app.core.deps import get_current_user
from app.models.user import User, UserRole
from tests.http_helpers import api, auth, make_org, make_user  # noqa: F401
from tests.test_queue_atomicity import real_redis  # noqa: F401  (fixture importée par nom)


def _token_expire(user_id: str) -> str:
    """Jeton d'accès signé mais déjà expiré."""
    payload = {
        "sub": user_id,
        "type": "access",
        "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
        "iat": datetime.now(timezone.utc) - timedelta(hours=1),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


# ══ La dépendance, isolée (ne demande pas Redis) ═════════════════════════════

@pytest.mark.asyncio
async def test_dependance_sans_entete_leve_401_et_demande_un_jeton(db_session):
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(credentials=None, db=db_session)

    erreur = exc_info.value
    assert erreur.status_code == 401
    assert erreur.detail["code"] == "MISSING_TOKEN"
    # Sans cet en-tête, un client HTTP conforme n'affiche aucune invite de
    # connexion et ne sait pas qu'il doit présenter un jeton.
    assert erreur.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_dependance_avec_jeton_invalide_leve_401(db_session):
    identifiants = HTTPAuthorizationCredentials(scheme="Bearer", credentials="pas-un-jwt")
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(credentials=identifiants, db=db_session)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "INVALID_TOKEN"


@pytest.mark.asyncio
async def test_dependance_avec_jeton_valide_charge_l_utilisateur(db_session):
    """Le chemin nominal n'a pas bougé : on ne casse pas l'authentification en la corrigeant."""
    user = User(phone=f"+257{uuid.uuid4().int % 10**8:08d}", role=UserRole.CLIENT, is_verified=True)
    db_session.add(user)
    await db_session.commit()

    from app.core.security import create_access_token

    identifiants = HTTPAuthorizationCredentials(
        scheme="Bearer", credentials=create_access_token(user.id)
    )
    charge = await get_current_user(credentials=identifiants, db=db_session)
    assert charge.id == user.id


# ══ Bout en bout : ce que voit réellement le mobile ══════════════════════════

@pytest_asyncio.fixture
async def mini_app(db_session):
    """
    Application minimale avec UNE route protégée par la vraie dépendance.

    Permet de tester le comportement HTTP de `HTTPBearer` sans monter toute
    l'application (donc sans Redis) : c'est exactement le mécanisme qui a changé,
    et le test tourne partout, y compris en local.
    """
    from fastapi import FastAPI

    from app.core.database import get_db

    application = FastAPI()

    @application.get("/protege")
    async def protege(user: User = Depends(get_current_user)):
        return {"user_id": user.id}

    async def _session():
        yield db_session

    application.dependency_overrides[get_db] = _session

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_route_protegee_sans_entete_renvoie_401_sur_le_mecanisme_reel(mini_app):
    """
    Le cœur du correctif, sans Redis : FastAPI ne doit plus court-circuiter en 403
    quand l'en-tête est absent, c'est notre dépendance qui doit répondre 401.
    """
    response = await mini_app.get("/protege")

    assert response.status_code == 401, "un jeton absent n'est pas une interdiction"
    assert response.json()["detail"]["code"] == "MISSING_TOKEN"
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_schema_d_authentification_non_bearer_renvoie_401_sur_le_mecanisme_reel(mini_app):
    """
    `Authorization: Basic …` : `HTTPBearer` ne reconnaît pas le schéma et renvoie
    None. Sans le correctif, ce cas donnait aussi 403.
    """
    response = await mini_app.get("/protege", headers={"Authorization": "Basic dXNlcjpwYXNz"})

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "MISSING_TOKEN"


@pytest.mark.asyncio
async def test_bearer_vide_renvoie_401_sur_le_mecanisme_reel(mini_app):
    response = await mini_app.get("/protege", headers={"Authorization": "Bearer "})

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "MISSING_TOKEN"


@pytest.mark.asyncio
async def test_openapi_conserve_le_schema_bearer_sur_le_mecanisme_reel(mini_app):
    """
    `auto_error=False` ne doit pas retirer le schéma de sécurité de l'OpenAPI :
    sans lui, Swagger n'affiche plus le bouton « Authorize » et tous les essais
    manuels renvoient 401 — un correctif qui casse la documentation n'en est pas un.
    """
    schema = (await mini_app.get("/openapi.json")).json()

    schemes = schema["components"]["securitySchemes"]
    assert any(
        definition.get("type") == "http" and definition.get("scheme") == "bearer"
        for definition in schemes.values()
    ), schemes
    assert schema["paths"]["/protege"]["get"].get("security"), "route non marquée comme protégée"


# ── Les mêmes cas à travers l'application complète (CI : Redis requis) ───────

@pytest.mark.asyncio
async def test_route_protegee_sans_entete_renvoie_401(api):
    """Le cas qui cassait le mobile : aucune en-tête du tout, sur la vraie app."""
    response = await api.get("/api/v1/auth/me")

    assert response.status_code == 401, "un jeton absent n'est pas une interdiction"
    assert response.json()["detail"]["code"] == "MISSING_TOKEN"
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_jeton_invalide_renvoie_401(api):
    response = await api.get("/api/v1/auth/me", headers={"Authorization": "Bearer pas-un-jwt"})

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "INVALID_TOKEN"


@pytest.mark.asyncio
async def test_jeton_expire_renvoie_401(api, db_session):
    user = await make_user(db_session, role=UserRole.CLIENT)

    response = await api.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {_token_expire(user.id)}"}
    )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "INVALID_TOKEN"


@pytest.mark.asyncio
async def test_role_insuffisant_reste_403(api, db_session):
    """
    La distinction est le but du correctif : un client authentifié qui demande une
    route d'administration doit recevoir 403, PAS 401. Se reconnecter n'y changerait
    rien — et un 401 le ferait tourner en boucle sur l'écran de connexion.
    """
    client = await make_user(db_session, role=UserRole.CLIENT)

    response = await api.get("/health/detailed", headers=auth(client))

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_route_organisation_reservee_aux_admins_reste_403(api, db_session):
    """
    Un client authentifié sur un export réservé aux superviseurs : 403, jamais 401.
    Se reconnecter ne changerait rien, donc l'écran de connexion ne doit pas
    s'afficher.
    """
    org = await make_org(db_session)
    client = await make_user(db_session, role=UserRole.CLIENT)

    response = await api.get(
        f"/api/v1/organizations/{org.id}/tickets/export", headers=auth(client)
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FORBIDDEN"


# ══ Swagger ne doit pas perdre le cadenas ════════════════════════════════════

@pytest.mark.asyncio
async def test_openapi_de_l_application_complete_conserve_le_schema_bearer(api):
    """Même vérification, sur le schéma réel exposé par l'application."""
    schema = (await api.get("/openapi.json")).json()

    schemes = schema["components"]["securitySchemes"]
    assert any(
        definition.get("type") == "http" and definition.get("scheme") == "bearer"
        for definition in schemes.values()
    ), schemes

    profil = schema["paths"]["/api/v1/auth/me"]["get"]
    assert profil.get("security"), "la route /auth/me doit rester marquée comme protégée"
