"""
Tests unitaires — EmployeeService

Couvre :
  - Création : cas nominal, user inexistant, doublon dans org
  - Get by ID : cas nominal, isolation multi-tenant
  - List : filtrage, pagination
  - Update : rôle, statut
  - Deactivate (soft) : remet User au rôle CLIENT
  - Isolation multi-tenant : org A ne voit pas les employés d'org B
"""
import pytest
from fastapi import HTTPException

from app.services.employee_service import EmployeeService
from app.services.organization_service import OrganizationService
from app.schemas.employee import CreateEmployeeRequest, UpdateEmployeeRequest
from app.schemas.organization import CreateOrganizationRequest
from app.models.user import User, UserRole
from app.models.employee import EmployeeStatus


# ── Fixtures helpers ──────────────────────────────────────────────────────────

@pytest.fixture
def employee_service(db_session):
    return EmployeeService(db_session)


@pytest.fixture
def org_service(db_session):
    return OrganizationService(db_session)


async def _create_org(org_service, slug: str = "bcb", name: str = "BCB") -> str:
    org = await org_service.create(CreateOrganizationRequest(
        name=name, slug=slug, sector="bank"
    ))
    return org.id


async def _create_user(db_session, email: str = "agent@test.bi", name: str = "Agent Test") -> str:
    """Crée un utilisateur actif sans passer par AuthService."""
    user = User(email=email, name=name, is_active=True, is_verified=True)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user.id


def _caller(org_id: str, role: UserRole = UserRole.ADMIN_ORG) -> User:
    """
    Utilisateur appelant factice.

    EmployeeService ne lit que `role` et `org_id` sur l'appelant : inutile de
    persister cette instance. Le rôle détermine les rôles attribuables.
    """
    return User(id=f"caller-{role.value}", org_id=org_id, role=role)


# ── Create ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_employee_success(employee_service, org_service, db_session):
    """Création d'un employé et mise à jour du rôle User."""
    org_id = await _create_org(org_service)
    user_id = await _create_user(db_session, email="jean@test.bi", name="Jean")

    result = await employee_service.create(
        org_id,
        CreateEmployeeRequest(user_id=user_id, role=UserRole.AGENT),
        caller=_caller(org_id),
    )

    assert result.org_id == org_id
    assert result.user_id == user_id
    assert result.role == "agent"
    assert result.status == "active"


@pytest.mark.asyncio
async def test_create_employee_updates_user_role(employee_service, org_service, db_session):
    """Créer un employé met à jour le champ role du User."""
    from sqlalchemy import select
    org_id = await _create_org(org_service)
    user_id = await _create_user(db_session, email="role@test.bi")

    await employee_service.create(
        org_id,
        CreateEmployeeRequest(user_id=user_id, role=UserRole.SUPERVISOR),
        caller=_caller(org_id),
    )

    result = await db_session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one()
    assert user.role == UserRole.SUPERVISOR
    assert user.org_id == org_id


@pytest.mark.asyncio
async def test_create_employee_user_not_found_raises_404(employee_service, org_service):
    """User inexistant → HTTP 404."""
    org_id = await _create_org(org_service)

    with pytest.raises(HTTPException) as exc_info:
        await employee_service.create(
            org_id,
            CreateEmployeeRequest(user_id="bad-user-id", role=UserRole.AGENT),
            caller=_caller(org_id),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["code"] == "USER_NOT_FOUND"


@pytest.mark.asyncio
async def test_create_employee_duplicate_raises_409(employee_service, org_service, db_session):
    """Même user déjà employé dans la même org → HTTP 409."""
    org_id = await _create_org(org_service)
    user_id = await _create_user(db_session, email="dup@test.bi")

    await employee_service.create(org_id, CreateEmployeeRequest(user_id=user_id, role=UserRole.AGENT), caller=_caller(org_id))

    with pytest.raises(HTTPException) as exc_info:
        await employee_service.create(org_id, CreateEmployeeRequest(user_id=user_id, role=UserRole.AGENT), caller=_caller(org_id))

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "EMPLOYEE_EXISTS"


@pytest.mark.asyncio
async def test_create_employee_client_role_raises_validation_error(org_service):
    """Role CLIENT interdit pour un employé → ValidationError Pydantic."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CreateEmployeeRequest(user_id="some-id", role=UserRole.CLIENT)


# ── Get by ID ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_employee_by_id_success(employee_service, org_service, db_session):
    """Récupère un employé par son ID."""
    org_id = await _create_org(org_service)
    user_id = await _create_user(db_session, email="get@test.bi")
    created = await employee_service.create(org_id, CreateEmployeeRequest(user_id=user_id, role=UserRole.AGENT), caller=_caller(org_id))

    result = await employee_service.get_by_id(created.id, org_id)
    assert result.id == created.id
    assert result.user_id == user_id


@pytest.mark.asyncio
async def test_get_employee_wrong_org_raises_404(employee_service, org_service, db_session):
    """Un admin d'org B ne peut pas voir un employé d'org A."""
    org_a = await _create_org(org_service, slug="org-a", name="Org A")
    org_b = await _create_org(org_service, slug="org-b", name="Org B")
    user_id = await _create_user(db_session, email="isolation@test.bi")
    created = await employee_service.create(org_a, CreateEmployeeRequest(user_id=user_id, role=UserRole.AGENT), caller=_caller(org_a))

    with pytest.raises(HTTPException) as exc_info:
        await employee_service.get_by_id(created.id, org_b)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["code"] == "EMPLOYEE_NOT_FOUND"


@pytest.mark.asyncio
async def test_get_employee_not_found_raises_404(employee_service, org_service):
    """ID inexistant → HTTP 404."""
    org_id = await _create_org(org_service)

    with pytest.raises(HTTPException) as exc_info:
        await employee_service.get_by_id("bad-id", org_id)

    assert exc_info.value.status_code == 404


# ── List ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_employees_filtered_by_org(employee_service, org_service, db_session):
    """list() retourne uniquement les employés de l'org demandée."""
    org_a = await _create_org(org_service, slug="list-a", name="List A")
    org_b = await _create_org(org_service, slug="list-b", name="List B")

    user_1 = await _create_user(db_session, email="u1@test.bi")
    user_2 = await _create_user(db_session, email="u2@test.bi")
    user_3 = await _create_user(db_session, email="u3@test.bi")

    await employee_service.create(org_a, CreateEmployeeRequest(user_id=user_1, role=UserRole.AGENT), caller=_caller(org_a))
    await employee_service.create(org_a, CreateEmployeeRequest(user_id=user_2, role=UserRole.SUPERVISOR), caller=_caller(org_a))
    await employee_service.create(org_b, CreateEmployeeRequest(user_id=user_3, role=UserRole.AGENT), caller=_caller(org_b))

    result_a = await employee_service.list(org_id=org_a)
    assert result_a.total == 2

    result_b = await employee_service.list(org_id=org_b)
    assert result_b.total == 1


@pytest.mark.asyncio
async def test_list_employees_filter_by_role(employee_service, org_service, db_session):
    """list() filtre par rôle."""
    org_id = await _create_org(org_service)
    u1 = await _create_user(db_session, email="agent1@test.bi")
    u2 = await _create_user(db_session, email="super1@test.bi")
    await employee_service.create(org_id, CreateEmployeeRequest(user_id=u1, role=UserRole.AGENT), caller=_caller(org_id))
    await employee_service.create(org_id, CreateEmployeeRequest(user_id=u2, role=UserRole.SUPERVISOR), caller=_caller(org_id))

    agents = await employee_service.list(org_id=org_id, role=UserRole.AGENT)
    assert agents.total == 1
    assert agents.items[0].role == "agent"


# ── Update ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_employee_role(employee_service, org_service, db_session):
    """Mise à jour du rôle d'un employé."""
    org_id = await _create_org(org_service)
    user_id = await _create_user(db_session, email="upd@test.bi")
    created = await employee_service.create(org_id, CreateEmployeeRequest(user_id=user_id, role=UserRole.AGENT), caller=_caller(org_id))

    updated = await employee_service.update(
        created.id, org_id,
        UpdateEmployeeRequest(role=UserRole.SUPERVISOR),
        caller=_caller(org_id),
    )
    assert updated.role == "supervisor"


@pytest.mark.asyncio
async def test_update_employee_wrong_org_raises_404(employee_service, org_service, db_session):
    """Un admin d'org B ne peut pas modifier un employé d'org A."""
    org_a = await _create_org(org_service, slug="upd-a", name="Upd A")
    org_b = await _create_org(org_service, slug="upd-b", name="Upd B")
    user_id = await _create_user(db_session, email="updiso@test.bi")
    created = await employee_service.create(org_a, CreateEmployeeRequest(user_id=user_id, role=UserRole.AGENT), caller=_caller(org_a))

    with pytest.raises(HTTPException) as exc_info:
        await employee_service.update(
            created.id, org_b,
            UpdateEmployeeRequest(role=UserRole.SUPERVISOR),
            caller=_caller(org_b),
        )

    assert exc_info.value.status_code == 404


# ── Deactivate (soft delete) ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deactivate_employee_resets_user_to_client(employee_service, org_service, db_session):
    """Désactiver un employé remet le User au rôle CLIENT."""
    from sqlalchemy import select
    org_id = await _create_org(org_service)
    user_id = await _create_user(db_session, email="deact@test.bi")
    created = await employee_service.create(org_id, CreateEmployeeRequest(user_id=user_id, role=UserRole.AGENT), caller=_caller(org_id))

    await employee_service.deactivate(created.id, org_id)

    user_result = await db_session.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one()
    assert user.role == UserRole.CLIENT
    assert user.org_id is None


@pytest.mark.asyncio
async def test_deactivate_employee_excluded_from_list(employee_service, org_service, db_session):
    """Un employé désactivé n'apparaît plus dans list()."""
    org_id = await _create_org(org_service)
    user_id = await _create_user(db_session, email="excl@test.bi")
    created = await employee_service.create(org_id, CreateEmployeeRequest(user_id=user_id, role=UserRole.AGENT), caller=_caller(org_id))

    result_before = await employee_service.list(org_id=org_id)
    assert result_before.total == 1

    await employee_service.deactivate(created.id, org_id)

    result_after = await employee_service.list(org_id=org_id)
    assert result_after.total == 0


@pytest.mark.asyncio
async def test_deactivate_employee_wrong_org_raises_404(employee_service, org_service, db_session):
    """Un admin d'org B ne peut pas désactiver un employé d'org A."""
    org_a = await _create_org(org_service, slug="dea-a", name="Dea A")
    org_b = await _create_org(org_service, slug="dea-b", name="Dea B")
    user_id = await _create_user(db_session, email="deaiso@test.bi")
    created = await employee_service.create(org_a, CreateEmployeeRequest(user_id=user_id, role=UserRole.AGENT), caller=_caller(org_a))

    with pytest.raises(HTTPException) as exc_info:
        await employee_service.deactivate(created.id, org_b)

    assert exc_info.value.status_code == 404
