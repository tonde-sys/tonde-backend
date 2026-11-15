"""
Tests — changement de mot de passe par l'utilisateur connecté.

Avant cet endpoint, le seul moyen de changer un mot de passe était de relancer
`python -m app.cli bootstrap --reset-passwords` depuis un shell Render. Un agent
ne pouvait donc pas changer le mot de passe que son administrateur lui avait
transmis à la main — inacceptable dès qu'une organisation tierce reçoit des
comptes, et c'est exactement la situation du test partenaire ULBU.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from unittest.mock import patch, AsyncMock

from app.core.security import hash_password, verify_password
from app.models.refresh_token import RefreshToken
from app.models.user import User, UserRole
from app.schemas.auth import ChangePasswordRequest
from app.services.auth_service import AuthService

CURRENT = "MotDePasseTest-2026"


async def _user(db_session, email="agent1@ulbu.bi", password=CURRENT):
    user = User(
        email=email,
        name="Agent Guichet 1",
        hashed_password=hash_password(password) if password else None,
        is_active=True,
        is_verified=True,
        role=UserRole.AGENT,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _session(db_session, user, suffix="device-1"):
    token = RefreshToken(
        user_id=user.id,
        token_hash=f"{user.id[:8]}{suffix}".ljust(64, "0")[:64],
        device_id=suffix,
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    db_session.add(token)
    await db_session.commit()
    await db_session.refresh(token)
    return token


@pytest.fixture(autouse=True)
def no_redis():
    """Le compteur d'échecs vit dans Redis : neutralisé pour ces tests."""
    with patch("app.services.auth_service.clear_login_failures", new_callable=AsyncMock):
        yield


# ── Cas nominal ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_changement_reussi(auth_service, db_session):
    """Le nouveau mot de passe est haché, l'ancien ne fonctionne plus."""
    user = await _user(db_session)

    result = await auth_service.change_password(user, CURRENT, "Nouveau-Mot-De-Passe-2026")

    assert result["success"] is True
    await db_session.refresh(user)
    assert verify_password("Nouveau-Mot-De-Passe-2026", user.hashed_password)
    assert not verify_password(CURRENT, user.hashed_password)


@pytest.mark.asyncio
async def test_toutes_les_sessions_sont_revoquees(auth_service, db_session):
    """
    Un mot de passe changé doit invalider les refresh tokens émis avant.

    Sans cela, un token volé continuerait de fonctionner : changer le mot de
    passe ne protégerait de rien, ce qui est précisément la raison d'être de
    l'opération.
    """
    user = await _user(db_session)
    first = await _session(db_session, user, "poste-guichet")
    second = await _session(db_session, user, "telephone")

    result = await auth_service.change_password(user, CURRENT, "Nouveau-Mot-De-Passe-2026")

    assert result["revoked_sessions"] == 2
    await db_session.refresh(first)
    await db_session.refresh(second)
    assert first.revoked_at is not None
    assert second.revoked_at is not None


@pytest.mark.asyncio
async def test_sessions_deja_revoquees_non_recomptees(auth_service, db_session):
    """Une session déjà fermée ne doit pas gonfler le compteur retourné."""
    user = await _user(db_session)
    await _session(db_session, user, "ancien-poste")
    await auth_service.logout_all(user.id)

    result = await auth_service.change_password(user, CURRENT, "Nouveau-Mot-De-Passe-2026")

    assert result["revoked_sessions"] == 0


# ── Refus ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ancien_mot_de_passe_incorrect(auth_service, db_session):
    """Un access token volé ne suffit pas : il faut l'ancien mot de passe."""
    user = await _user(db_session)

    with pytest.raises(HTTPException) as exc_info:
        await auth_service.change_password(user, "mauvais", "Nouveau-Mot-De-Passe-2026")

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "INVALID_CURRENT_PASSWORD"
    await db_session.refresh(user)
    assert verify_password(CURRENT, user.hashed_password)


@pytest.mark.asyncio
async def test_nouveau_identique_a_l_ancien(auth_service, db_session):
    """Réutiliser le même mot de passe est refusé explicitement."""
    user = await _user(db_session)

    with pytest.raises(HTTPException) as exc_info:
        await auth_service.change_password(user, CURRENT, CURRENT)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "PASSWORD_UNCHANGED"


@pytest.mark.asyncio
async def test_compte_sans_mot_de_passe(auth_service, db_session):
    """Un client inscrit par OTP n'a pas de mot de passe : 409, pas 400."""
    user = await _user(db_session, email="client@example.com", password=None)

    with pytest.raises(HTTPException) as exc_info:
        await auth_service.change_password(user, "peu-importe", "Nouveau-Mot-De-Passe-2026")

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "PASSWORD_NOT_SET"


@pytest.mark.asyncio
async def test_echec_ne_revoque_pas_les_sessions(auth_service, db_session):
    """Un refus ne doit pas déconnecter l'utilisateur : rien n'a changé."""
    user = await _user(db_session)
    session = await _session(db_session, user)

    with pytest.raises(HTTPException):
        await auth_service.change_password(user, "mauvais", "Nouveau-Mot-De-Passe-2026")

    await db_session.refresh(session)
    assert session.revoked_at is None


# ── Validation du schéma ─────────────────────────────────────────────────────

def test_mot_de_passe_trop_court_refuse():
    with pytest.raises(ValidationError):
        ChangePasswordRequest(current_password=CURRENT, new_password="court")


def test_mot_de_passe_au_dela_de_72_octets_refuse():
    """
    bcrypt tronque silencieusement au-delà de 72 octets.

    Sans ce refus, deux mots de passe partageant les 72 premiers octets sont
    équivalents : l'utilisateur croit avoir ajouté du secret, il n'a rien
    ajouté. Mieux vaut le dire que le taire.
    """
    with pytest.raises(ValidationError):
        ChangePasswordRequest(current_password=CURRENT, new_password="a" * 73)


def test_mot_de_passe_accepte_a_la_limite():
    """72 octets pile reste accepté : la limite est inclusive."""
    body = ChangePasswordRequest(current_password=CURRENT, new_password="a" * 72)
    assert len(body.new_password.encode("utf-8")) == 72


# ── Câblage de la route ──────────────────────────────────────────────────────

def test_route_exposee_et_protegee():
    """
    La route doit exister ET exiger un Bearer.

    Ce test ne dépend ni de PostgreSQL ni de Redis : il attrape la régression
    la plus probable (route oubliée dans le routeur, ou `get_current_user`
    retiré de la signature lors d'un refactor).
    """
    from app.main import app

    matches = [
        route for route in app.routes
        if getattr(route, "path", None) == "/api/v1/auth/change-password"
    ]
    assert len(matches) == 1
    route = matches[0]
    assert "POST" in route.methods
    # La dépendance d'authentification apparaît dans les dépendances résolues.
    assert any(
        getattr(dep.call, "__name__", "") == "get_current_user"
        for dep in route.dependant.dependencies
    )
