"""
Configuration pytest pour TONDE Backend.

Fixtures disponibles :
  - db_session : session DB asynchrone en mémoire (SQLite)
  - mock_redis : Redis mocké (pas de vraie connexion)
  - auth_service : AuthService avec DB de test
  - ticket_service : TicketService avec DB de test

Utilisation dans un test :
    async def test_register_phone(auth_service):
        result = await auth_service.register_phone(...)
        assert result["success"] is True
"""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
# Importer tous les modèles pour que Base.metadata les connaisse
import app.models  # noqa: F401

from app.services.auth_service import AuthService
from app.services.ticket_service import TicketService
from app.core.redis import close_redis


@pytest_asyncio.fixture(autouse=True)
async def close_test_redis_on_its_loop():
    """Function-scoped client teardown before pytest closes the test loop.

    Async tests use the same function-scoped loop for fixtures and test body.
    Do not merely clear redis_client: that leaks open sockets on a dead loop.
    Production's connection lifetime remains managed by app.main lifespan.
    """
    yield
    await close_redis()


# ── Configuration SQLite en mémoire ──────────────────────────────────────────
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(scope="function")
async def db_engine():
    """
    Crée un moteur SQLite en mémoire pour les tests.
    Chaque test obtient une base fraîche.
    """
    engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_session(db_engine):
    """Fournit une session DB de test."""
    session_factory = async_sessionmaker(
        db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session


@pytest.fixture
def mock_redis():
    """
    Mock complet du client Redis pour les tests.
    Couvre toutes les fonctions utilisées par auth_service et ticket_service :
      - get/setex/delete/incr : OTP, compteurs
      - ttl : vérification TTL pour is_phone_blocked
      - zadd/zrank/zcard/zrange/zrem : file d'attente tickets
      - ping : health check
    """
    with patch("app.core.redis.get_redis") as mock:
        redis_mock = AsyncMock()
        redis_mock.get = AsyncMock(return_value=None)
        redis_mock.setex = AsyncMock(return_value=True)
        redis_mock.delete = AsyncMock(return_value=1)
        redis_mock.incr = AsyncMock(return_value=1)
        redis_mock.expire = AsyncMock(return_value=True)
        redis_mock.ttl = AsyncMock(return_value=-2)   # -2 = clé absente (non bloqué)
        redis_mock.zadd = AsyncMock(return_value=1)
        redis_mock.zrank = AsyncMock(return_value=0)
        redis_mock.zcard = AsyncMock(return_value=1)
        redis_mock.zrange = AsyncMock(return_value=[])
        redis_mock.zrem = AsyncMock(return_value=1)
        redis_mock.ping = AsyncMock(return_value=True)
        redis_mock.publish = AsyncMock(return_value=1)
        mock.return_value = redis_mock
        yield redis_mock


@pytest_asyncio.fixture
async def auth_service(db_session):
    """AuthService prêt à l'emploi avec DB de test."""
    return AuthService(db_session)


@pytest_asyncio.fixture
async def ticket_service(db_session):
    """TicketService prêt à l'emploi avec DB de test."""
    return TicketService(db_session)
