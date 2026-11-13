"""
Outils partagés des tests HTTP de bout en bout.

La fixture `api` monte l'application COMPLÈTE (routers, middlewares, gestionnaires d'erreurs,
authentification JWT réelle) devant une base SQLite en mémoire et un VRAI Redis. Aucun
service n'est mocké : on teste ce que le mobile et le desktop appellent réellement.

Les composants qui ouvrent leur propre session (export en streaming, WebSocket, sondes de
santé) sont redirigés vers la base de test.
"""
import uuid

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import get_db
from app.core.security import create_access_token
from app.models.branch import Branch, Service
from app.models.counter import Counter
from app.models.employee import Employee
from app.models.organization import Organization
from app.models.user import User, UserRole
from tests.test_queue_atomicity import real_redis  # noqa: F401  (fixture importée par nom)


@pytest_asyncio.fixture
async def api(db_engine, real_redis, monkeypatch):
    """Client HTTP sur l'application complète (base de test + vrai Redis)."""
    from app.main import app
    from app.routers import exports as exports_router
    from app.routers import tickets as tickets_router
    from app.services import health_service
    from app.services.export_service import ExportService

    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(exports_router, "ExportService", lambda: ExportService(session_factory=factory))
    monkeypatch.setattr(tickets_router, "AsyncSessionLocal", factory)
    original_check_database = health_service.check_database
    monkeypatch.setattr(health_service, "check_database", lambda *_a, **_k: original_check_database(factory))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


def auth(user: User) -> dict[str, str]:
    """En-tête Authorization avec un vrai JWT d'accès pour cet utilisateur."""
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


async def make_user(db, role: UserRole = UserRole.CLIENT, org_id: str | None = None, **fields) -> User:
    """Crée un utilisateur (téléphone unique)."""
    user = User(phone=f"+257{uuid.uuid4().int % 10**8:08d}", role=role, org_id=org_id,
                is_verified=True, **fields)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def make_org(db, slug: str | None = None) -> Organization:
    slug = slug or f"org-{uuid.uuid4().hex[:8]}"
    org = Organization(name=f"Banque {slug}", slug=slug, sector="bank")
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return org


async def make_branch(db, org_id: str, name: str = "Agence Centre", is_open: bool = True) -> Branch:
    branch = Branch(org_id=org_id, name=name, slug=f"b-{uuid.uuid4().hex[:8]}", sector="bank",
                    is_active=True, is_open=is_open)
    db.add(branch)
    await db.commit()
    await db.refresh(branch)
    return branch


async def make_service(db, org_id: str, branch_id: str, prefix: str = "A", name: str = "Dépôt") -> Service:
    service = Service(org_id=org_id, branch_id=branch_id, name=name, ticket_prefix=prefix, avg_duration_minutes=5)
    db.add(service)
    await db.commit()
    await db.refresh(service)
    return service


async def make_counter(db, org_id: str, branch_id: str, name: str = "Guichet 1", is_open: bool = True) -> Counter:
    counter = Counter(org_id=org_id, agency_id=branch_id, name=name, is_open=is_open)
    db.add(counter)
    await db.commit()
    await db.refresh(counter)
    return counter


async def make_staff(db, org_id: str, role: UserRole, branch_id: str | None = None,
                     counter_id: str | None = None) -> User:
    """Utilisateur staff ; rattaché à une branch / un guichet via un Employee actif."""
    user = await make_user(db, role=role, org_id=org_id)
    if branch_id or counter_id:
        db.add(Employee(org_id=org_id, user_id=user.id, agency_id=branch_id, counter_id=counter_id, role=role))
        await db.commit()
    return user


async def make_world(db):
    """Org + branche ouverte + service A + guichet ouvert + agent affecté + client."""
    org = await make_org(db)
    branch = await make_branch(db, org.id)
    service = await make_service(db, org.id, branch.id)
    counter = await make_counter(db, org.id, branch.id)
    agent = await make_staff(db, org.id, UserRole.AGENT, branch.id, counter.id)
    client = await make_user(db)
    return org, branch, service, counter, agent, client
