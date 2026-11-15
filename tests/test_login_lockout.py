"""
Tests — verrouillage de connexion par COMPTE (P1-6 de l'audit du 15/11).

Le rate limit du routeur est indexé sur l'IP (`10/minute`). Un attaquant qui
dispose de plusieurs IP contourne entièrement cette protection : 100 IP × 10
tentatives = 1 000 essais par minute sur le même compte. Les comptes staff
d'une banque sont exactement ce qu'il faut protéger.

Ces tests couvrent les deux niveaux :
  - la mécanique Redis (compteur, paliers progressifs, remise à zéro) ;
  - le comportement du service (429 avant vérification, 401 qui comptabilise,
    connexion réussie qui remet à zéro, et Redis en panne qui ne bloque rien).
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from unittest.mock import patch, AsyncMock

from app.core.redis import (
    LOGIN_BACKOFF_LADDER,
    LOGIN_MAX_ATTEMPTS,
    get_login_block,
    register_login_failure,
    clear_login_failures,
)
from app.core.security import hash_password
from app.models.user import User, UserRole
from app.schemas.auth import LoginEmailRequest
from app.services.auth_service import AuthService


# ── Faux client Redis, en mémoire ────────────────────────────────────────────
class FakeRedis:
    """
    Redis minimal : seulement les commandes utilisées par le verrouillage.

    Un AsyncMock ne suffit pas ici : il faut que `incr` retourne vraiment
    1, 2, 3… pour vérifier que le blocage se déclenche au 5ᵉ échec et pas au
    premier. Un mock à valeur constante testerait le mock, pas la logique.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        value = int(self.store.get(key, "0")) + 1
        self.store[key] = str(value)
        return value

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        return True

    async def setex(self, key: str, seconds: int, value: str) -> bool:
        self.store[key] = value
        self.ttls[key] = seconds
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                removed += 1
            self.ttls.pop(key, None)
        return removed

    async def ttl(self, key: str) -> int:
        if key not in self.store:
            return -2
        return self.ttls.get(key, -1)


@pytest.fixture
def fake_redis():
    fake = FakeRedis()
    with patch("app.core.redis.get_redis", new_callable=AsyncMock, return_value=fake):
        yield fake


# ── Mécanique Redis ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pas_de_blocage_avant_le_seuil(fake_redis):
    """Les quatre premiers échecs ne bloquent pas : le compteur monte seulement."""
    for attendu in range(1, LOGIN_MAX_ATTEMPTS):
        attempts, blocked_for = await register_login_failure("agent1@ulbu.bi")
        assert attempts == attendu
        assert blocked_for == 0
        assert await get_login_block("agent1@ulbu.bi") == 0


@pytest.mark.asyncio
async def test_blocage_au_cinquieme_echec(fake_redis):
    """Le 5ᵉ échec pose un blocage d'une minute (premier palier)."""
    for _ in range(LOGIN_MAX_ATTEMPTS - 1):
        await register_login_failure("agent1@ulbu.bi")

    attempts, blocked_for = await register_login_failure("agent1@ulbu.bi")

    assert attempts == LOGIN_MAX_ATTEMPTS
    assert blocked_for == LOGIN_BACKOFF_LADDER[0] == 60
    assert await get_login_block("agent1@ulbu.bi") == 60


@pytest.mark.asyncio
async def test_paliers_progressifs(fake_redis):
    """5 échecs → 1 min, 5 de plus → 5 min, 5 de plus → 15 min, puis plafond."""
    durees = []
    for _ in range(LOGIN_MAX_ATTEMPTS * (len(LOGIN_BACKOFF_LADDER) + 1)):
        _, blocked_for = await register_login_failure("agent1@ulbu.bi")
        if blocked_for:
            durees.append(blocked_for)

    assert durees[: len(LOGIN_BACKOFF_LADDER)] == list(LOGIN_BACKOFF_LADDER)
    # Au-delà du dernier palier, on reste à 15 min : pas de blocage infini.
    assert durees[-1] == LOGIN_BACKOFF_LADDER[-1]


@pytest.mark.asyncio
async def test_email_insensible_a_la_casse(fake_redis):
    """Agent1@ULBU.bi et agent1@ulbu.bi sont le même compte : même clé Redis."""
    for _ in range(LOGIN_MAX_ATTEMPTS):
        await register_login_failure("Agent1@ULBU.bi")

    assert await get_login_block("agent1@ulbu.bi") == 60


@pytest.mark.asyncio
async def test_cle_de_connexion_reussie_efface_tout(fake_redis):
    """Une connexion réussie annule compteur, paliers et blocage."""
    for _ in range(LOGIN_MAX_ATTEMPTS):
        await register_login_failure("agent1@ulbu.bi")
    assert await get_login_block("agent1@ulbu.bi") > 0

    await clear_login_failures("agent1@ulbu.bi")

    assert await get_login_block("agent1@ulbu.bi") == 0
    # Le compteur de paliers repart aussi de zéro : pas de pénalité conservée
    # pour un utilisateur qui s'est trompé puis a retrouvé son mot de passe.
    _, blocked_for = await register_login_failure("agent1@ulbu.bi")
    assert blocked_for == 0


# ── Comportement du service ──────────────────────────────────────────────────

async def _user_with_password(db_session, email="agent1@ulbu.bi", password="MotDePasseTest-2026"):
    user = User(
        email=email,
        name="Agent Guichet 1",
        hashed_password=hash_password(password),
        is_active=True,
        is_verified=True,
        role=UserRole.AGENT,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.mark.asyncio
async def test_compte_bloque_refuse_avant_de_verifier_le_mot_de_passe(auth_service):
    """
    Un compte bloqué renvoie 429 même avec le BON mot de passe.

    C'est le point du verrouillage : sinon un attaquant qui finit par trouver
    le mot de passe passe outre, et le blocage ne sert qu'à ralentir.
    """
    with patch(
        "app.services.auth_service.get_login_block",
        new_callable=AsyncMock,
        return_value=42,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await auth_service.login_email(
                LoginEmailRequest(email="agent1@ulbu.bi", password="MotDePasseTest-2026")
            )

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail["code"] == "ACCOUNT_LOCKED"
    assert exc_info.value.detail["retry_after_seconds"] == 42
    assert exc_info.value.headers["Retry-After"] == "42"


@pytest.mark.asyncio
async def test_mot_de_passe_faux_comptabilise_l_echec(auth_service, db_session):
    """Un échec d'authentification doit être enregistré, pas seulement renvoyé."""
    await _user_with_password(db_session)

    with patch(
        "app.services.auth_service.register_login_failure",
        new_callable=AsyncMock,
        return_value=(3, 0),
    ) as register:
        with pytest.raises(HTTPException) as exc_info:
            await auth_service.login_email(
                LoginEmailRequest(email="agent1@ulbu.bi", password="mauvais")
            )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "INVALID_CREDENTIALS"
    register.assert_awaited_once_with("agent1@ulbu.bi")


@pytest.mark.asyncio
async def test_email_inconnu_comptabilise_aussi(auth_service):
    """
    Un email inexistant compte comme un échec.

    Sinon le compteur ne protégerait que les comptes existants, et l'attaquant
    pourrait énumérer les emails en comparant les temps de réponse.
    """
    with patch(
        "app.services.auth_service.register_login_failure",
        new_callable=AsyncMock,
        return_value=(1, 0),
    ) as register:
        with pytest.raises(HTTPException):
            await auth_service.login_email(
                LoginEmailRequest(email="inconnu@ulbu.bi", password="x")
            )

    register.assert_awaited_once_with("inconnu@ulbu.bi")


@pytest.mark.asyncio
async def test_connexion_reussie_remet_le_compteur_a_zero(auth_service, db_session):
    """Le succès efface la pénalité : un agent qui se trompe 4 fois n'est pas puni."""
    await _user_with_password(db_session)

    with patch(
        "app.services.auth_service.clear_login_failures", new_callable=AsyncMock
    ) as clear:
        await auth_service.login_email(
            LoginEmailRequest(email="agent1@ulbu.bi", password="MotDePasseTest-2026")
        )

    clear.assert_awaited_once_with("agent1@ulbu.bi")


@pytest.mark.asyncio
async def test_redis_en_panne_ne_bloque_pas_la_connexion(auth_service, db_session):
    """
    Redis indisponible = pas de verrouillage, mais la connexion reste possible.

    Arbitrage assumé : couper l'accès de toute une agence parce que le cache est
    tombé serait pire que le risque couvert. La panne est journalisée.
    """
    await _user_with_password(db_session)

    with patch(
        "app.services.auth_service.get_login_block",
        new_callable=AsyncMock,
        side_effect=ConnectionError("redis down"),
    ):
        with patch(
            "app.services.auth_service.clear_login_failures",
            new_callable=AsyncMock,
            side_effect=ConnectionError("redis down"),
        ):
            result = await auth_service.login_email(
                LoginEmailRequest(email="agent1@ulbu.bi", password="MotDePasseTest-2026")
            )

    assert result.access_token


@pytest.mark.asyncio
async def test_compteur_en_panne_ne_casse_pas_le_401(auth_service, db_session):
    """Si Redis tombe pendant l'échec, le client reçoit quand même son 401."""
    await _user_with_password(db_session)

    with patch(
        "app.services.auth_service.register_login_failure",
        new_callable=AsyncMock,
        side_effect=ConnectionError("redis down"),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await auth_service.login_email(
                LoginEmailRequest(email="agent1@ulbu.bi", password="mauvais")
            )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "INVALID_CREDENTIALS"
