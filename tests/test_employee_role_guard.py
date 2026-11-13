"""
Tests de régression — Escalade de privilèges via la gestion des employés.

Bug corrigé : `CreateEmployeeRequest.role` acceptait SUPER_ADMIN et
`EmployeeService.create()` écrivait ce rôle directement sur le User.
Un simple ADMIN_AGENCY pouvait donc se promouvoir administrateur de toute
la plateforme, et écraser l'org_id d'un utilisateur d'une autre organisation.

Défenses en place :
  1. Schéma Pydantic  : SUPER_ADMIN et CLIENT ne sont jamais acceptés.
  2. Service          : on ne peut attribuer qu'un rôle inférieur au sien.
  3. Service          : un utilisateur d'une autre org ne peut pas être enrôlé.
  4. Service          : un compte SUPER_ADMIN est intouchable via cette API.
"""
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.services.employee_service import EmployeeService
from app.services.organization_service import OrganizationService
from app.schemas.employee import CreateEmployeeRequest, UpdateEmployeeRequest
from app.schemas.organization import CreateOrganizationRequest
from app.models.user import User, UserRole


# ── Helpers ───────────────────────────────────────────────────────────────────

@pytest.fixture
def employee_service(db_session):
    return EmployeeService(db_session)


@pytest.fixture
def org_service(db_session):
    return OrganizationService(db_session)


def _caller(org_id: str, role: UserRole = UserRole.ADMIN_AGENCY) -> User:
    """Appelant factice — le service ne lit que `role` et `org_id`."""
    return User(id=f"caller-{role.value}", org_id=org_id, role=role)


async def _create_org(org_service, slug: str, name: str) -> str:
    org = await org_service.create(
        CreateOrganizationRequest(name=name, slug=slug, sector="bank")
    )
    return org.id


async def _create_user(db_session, email: str, role: UserRole = UserRole.CLIENT, org_id=None) -> str:
    user = User(email=email, name=email, role=role, org_id=org_id, is_active=True)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user.id


async def _reload_user(db_session, user_id: str) -> User:
    from sqlalchemy import select
    result = await db_session.execute(select(User).where(User.id == user_id))
    return result.scalar_one()


# ── Barrière 1 : le schéma refuse les rôles interdits ────────────────────────

def test_schema_rejects_super_admin_role():
    """SUPER_ADMIN est refusé dès la validation Pydantic."""
    with pytest.raises(ValidationError):
        CreateEmployeeRequest(user_id="some-id", role=UserRole.SUPER_ADMIN)


def test_schema_rejects_super_admin_on_update():
    """Idem sur la mise à jour."""
    with pytest.raises(ValidationError):
        UpdateEmployeeRequest(role=UserRole.SUPER_ADMIN)


def test_schema_accepts_admin_org_role():
    """ADMIN_ORG reste accepté par le schéma : c'est le service qui arbitre selon l'appelant."""
    req = CreateEmployeeRequest(user_id="some-id", role=UserRole.ADMIN_ORG)
    assert req.role == UserRole.ADMIN_ORG


# ── Barrière 2 : un admin d'agence ne peut pas créer au-dessus de son niveau ─

@pytest.mark.asyncio
async def test_admin_agency_cannot_create_admin_org(employee_service, org_service, db_session):
    """ADMIN_AGENCY ne peut pas nommer un ADMIN_ORG → 403, aucun effet en base."""
    org_id = await _create_org(org_service, "esc-a", "Esc A")
    user_id = await _create_user(db_session, "cible-a@test.bi")

    with pytest.raises(HTTPException) as exc_info:
        await employee_service.create(
            org_id,
            CreateEmployeeRequest(user_id=user_id, role=UserRole.ADMIN_ORG),
            caller=_caller(org_id, UserRole.ADMIN_AGENCY),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "ROLE_NOT_ASSIGNABLE"

    # Le rôle de la cible n'a pas bougé
    user = await _reload_user(db_session, user_id)
    assert user.role == UserRole.CLIENT
    assert user.org_id is None


@pytest.mark.asyncio
async def test_admin_agency_can_create_supervisor(employee_service, org_service, db_session):
    """ADMIN_AGENCY peut nommer un SUPERVISOR (rôle inférieur) — cas nominal."""
    org_id = await _create_org(org_service, "esc-b", "Esc B")
    user_id = await _create_user(db_session, "cible-b@test.bi")

    result = await employee_service.create(
        org_id,
        CreateEmployeeRequest(user_id=user_id, role=UserRole.SUPERVISOR),
        caller=_caller(org_id, UserRole.ADMIN_AGENCY),
    )

    assert result.role == UserRole.SUPERVISOR.value


@pytest.mark.asyncio
async def test_super_admin_can_create_admin_org(employee_service, org_service, db_session):
    """SUPER_ADMIN peut nommer un ADMIN_ORG — le rôle le plus élevé attribuable."""
    org_id = await _create_org(org_service, "esc-c", "Esc C")
    user_id = await _create_user(db_session, "cible-c@test.bi")

    result = await employee_service.create(
        org_id,
        CreateEmployeeRequest(user_id=user_id, role=UserRole.ADMIN_ORG),
        caller=_caller(org_id, UserRole.SUPER_ADMIN),
    )

    assert result.role == UserRole.ADMIN_ORG.value


@pytest.mark.asyncio
async def test_agent_cannot_create_any_employee(employee_service, org_service, db_session):
    """Un AGENT n'a aucun rôle attribuable."""
    org_id = await _create_org(org_service, "esc-d", "Esc D")
    user_id = await _create_user(db_session, "cible-d@test.bi")

    with pytest.raises(HTTPException) as exc_info:
        await employee_service.create(
            org_id,
            CreateEmployeeRequest(user_id=user_id, role=UserRole.AGENT),
            caller=_caller(org_id, UserRole.AGENT),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "ROLE_NOT_ASSIGNABLE"


# ── Barrière 3 : pas d'absorption inter-tenant ───────────────────────────────

@pytest.mark.asyncio
async def test_cannot_enroll_user_from_another_org(employee_service, org_service, db_session):
    """Un user rattaché à l'org A ne peut pas être enrôlé dans l'org B → 409."""
    org_a = await _create_org(org_service, "abs-a", "Abs A")
    org_b = await _create_org(org_service, "abs-b", "Abs B")
    victim_id = await _create_user(
        db_session, "victime@test.bi", role=UserRole.SUPERVISOR, org_id=org_a
    )

    with pytest.raises(HTTPException) as exc_info:
        await employee_service.create(
            org_b,
            CreateEmployeeRequest(user_id=victim_id, role=UserRole.AGENT),
            caller=_caller(org_b, UserRole.ADMIN_ORG),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "USER_IN_ANOTHER_ORG"

    # La victime est toujours dans son organisation d'origine
    victim = await _reload_user(db_session, victim_id)
    assert victim.org_id == org_a
    assert victim.role == UserRole.SUPERVISOR


@pytest.mark.asyncio
async def test_cannot_enroll_super_admin(employee_service, org_service, db_session):
    """Un compte SUPER_ADMIN ne peut pas être enrôlé comme employé → 403."""
    org_id = await _create_org(org_service, "abs-c", "Abs C")
    root_id = await _create_user(db_session, "root@tonde.bi", role=UserRole.SUPER_ADMIN)

    with pytest.raises(HTTPException) as exc_info:
        await employee_service.create(
            org_id,
            CreateEmployeeRequest(user_id=root_id, role=UserRole.AGENT),
            caller=_caller(org_id, UserRole.SUPER_ADMIN),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "SUPER_ADMIN_PROTECTED"


# ── Barrière 4 : l'update ne permet pas de contourner les règles ─────────────

@pytest.mark.asyncio
async def test_update_cannot_promote_above_caller(employee_service, org_service, db_session):
    """ADMIN_AGENCY ne peut pas promouvoir un employé en ADMIN_ORG via PATCH."""
    org_id = await _create_org(org_service, "upg-a", "Upg A")
    user_id = await _create_user(db_session, "promu@test.bi")

    created = await employee_service.create(
        org_id,
        CreateEmployeeRequest(user_id=user_id, role=UserRole.AGENT),
        caller=_caller(org_id, UserRole.ADMIN_AGENCY),
    )

    with pytest.raises(HTTPException) as exc_info:
        await employee_service.update(
            created.id,
            org_id,
            UpdateEmployeeRequest(role=UserRole.ADMIN_ORG),
            caller=_caller(org_id, UserRole.ADMIN_AGENCY),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "ROLE_NOT_ASSIGNABLE"

    user = await _reload_user(db_session, user_id)
    assert user.role == UserRole.AGENT


@pytest.mark.asyncio
async def test_update_cannot_touch_super_admin(employee_service, org_service, db_session):
    """Un employé dont le User est SUPER_ADMIN est protégé contre toute modification."""
    org_id = await _create_org(org_service, "upg-b", "Upg B")
    root_id = await _create_user(db_session, "root2@tonde.bi", role=UserRole.SUPER_ADMIN)

    # Provisionnement direct (l'API refuse ce cas, on simule un état hérité)
    from app.models.employee import Employee, EmployeeStatus
    employee = Employee(org_id=org_id, user_id=root_id, role=UserRole.SUPER_ADMIN, status=EmployeeStatus.ACTIVE)
    db_session.add(employee)
    await db_session.commit()
    await db_session.refresh(employee)

    with pytest.raises(HTTPException) as exc_info:
        await employee_service.update(
            employee.id,
            org_id,
            UpdateEmployeeRequest(role=UserRole.AGENT),
            caller=_caller(org_id, UserRole.SUPER_ADMIN),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "SUPER_ADMIN_PROTECTED"
