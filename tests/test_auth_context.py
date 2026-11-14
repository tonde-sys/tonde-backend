"""
Tests — contexte de travail exposé au client (`/auth/me` et login).

Le poste guichet doit pouvoir se configurer seul. Sans ces informations, il
fallait saisir à la main l'organisation et le guichet, avec un risque réel :
un agent ouvrait un guichet auquel il n'était pas affecté et recevait
COUNTER_NOT_ASSIGNED.

`memberships` est une LISTE : un utilisateur peut être rattaché à plusieurs
organisations, c'est le client qui choisit.
"""
import pytest

from app.models.branch import Branch
from app.models.counter import Counter
from app.models.employee import Employee, EmployeeStatus
from app.models.organization import Organization
from app.models.user import User, UserRole
from app.services.auth_service import AuthService


async def _world(db_session):
    org = Organization(name="Banque Test", slug="banque-test", sector="bank")
    db_session.add(org)
    await db_session.commit()
    await db_session.refresh(org)

    branch = Branch(
        org_id=org.id,
        name="Agence Centre",
        slug=f"centre-{org.id[:8]}",
        sector="bank",
        is_active=True,
        is_open=True,
    )
    db_session.add(branch)
    await db_session.commit()
    await db_session.refresh(branch)

    counter = Counter(
        org_id=org.id,
        agency_id=branch.id,
        name="Guichet 1",
        is_open=True,
        is_active=True,
    )
    db_session.add(counter)
    await db_session.commit()
    await db_session.refresh(counter)

    return org, branch, counter


async def _staff(db_session, org, branch, counter, role=UserRole.AGENT, phone="+25779002001"):
    user = User(phone=phone, is_active=True, role=role, org_id=org.id)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    db_session.add(
        Employee(
            org_id=org.id,
            user_id=user.id,
            agency_id=branch.id,
            counter_id=counter.id,
            role=role,
            status=EmployeeStatus.ACTIVE,
        )
    )
    await db_session.commit()
    return user


@pytest.mark.asyncio
async def test_staff_token_exposes_org_branch_and_counter(db_session):
    """Un agent reçoit son organisation, sa branche et son guichet."""
    org, branch, counter = await _world(db_session)
    agent = await _staff(db_session, org, branch, counter)

    token = await AuthService(db_session).build_user_token(agent)

    assert token.org_id == org.id
    assert len(token.memberships) == 1
    membership = token.memberships[0]
    assert membership.org_id == org.id
    assert membership.branch_id == branch.id
    assert membership.counter_id == counter.id
    assert membership.role == UserRole.AGENT.value
    assert membership.status == EmployeeStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_client_token_has_no_membership(db_session):
    """Un client n'a aucun rattachement staff : la liste est vide, pas absente."""
    client = User(phone="+25779002002", is_active=True, role=UserRole.CLIENT, org_id=None)
    db_session.add(client)
    await db_session.commit()
    await db_session.refresh(client)

    token = await AuthService(db_session).build_user_token(client)

    assert token.org_id is None
    assert token.memberships == []


@pytest.mark.asyncio
async def test_super_admin_has_no_org_but_stays_staff(db_session):
    """
    SUPER_ADMIN : org_id = None (équipe Tonde).

    C'est précisément ce None qui cassait la lecture d'un ticket quand le
    routage se faisait sur la présence d'un org_id au lieu du rôle.
    """
    super_admin = User(
        phone="+25779002003", is_active=True, role=UserRole.SUPER_ADMIN, org_id=None
    )
    db_session.add(super_admin)
    await db_session.commit()
    await db_session.refresh(super_admin)

    token = await AuthService(db_session).build_user_token(super_admin)

    assert token.org_id is None
    assert token.role == UserRole.SUPER_ADMIN.value
    assert token.memberships == []


@pytest.mark.asyncio
async def test_inactive_employee_is_not_exposed(db_session):
    """Un rattachement désactivé ne doit pas être proposé au poste guichet."""
    org, branch, counter = await _world(db_session)
    agent = await _staff(db_session, org, branch, counter, phone="+25779002004")

    employee = (
        await db_session.execute(
            Employee.__table__.select().where(Employee.user_id == agent.id)
        )
    ).first()
    await db_session.execute(
        Employee.__table__.update()
        .where(Employee.id == employee.id)
        .values(status=EmployeeStatus.INACTIVE)
    )
    await db_session.commit()

    token = await AuthService(db_session).build_user_token(agent)
    assert token.memberships == []


@pytest.mark.asyncio
async def test_multiple_memberships_are_all_returned(db_session):
    """Multi-organisations : tous les rattachements actifs remontent."""
    org_a, branch_a, counter_a = await _world(db_session)
    agent = await _staff(db_session, org_a, branch_a, counter_a, phone="+25779002005")

    org_b = Organization(name="Hopital Test", slug="hopital-test-b", sector="hospital")
    db_session.add(org_b)
    await db_session.commit()
    await db_session.refresh(org_b)

    branch_b = Branch(
        org_id=org_b.id,
        name="Accueil B",
        slug=f"accueil-{org_b.id[:8]}",
        sector="hospital",
        is_active=True,
        is_open=True,
    )
    db_session.add(branch_b)
    await db_session.commit()
    await db_session.refresh(branch_b)

    db_session.add(
        Employee(
            org_id=org_b.id,
            user_id=agent.id,
            agency_id=branch_b.id,
            role=UserRole.SUPERVISOR,
            status=EmployeeStatus.ACTIVE,
        )
    )
    await db_session.commit()

    token = await AuthService(db_session).build_user_token(agent)

    assert len(token.memberships) == 2
    assert {m.org_id for m in token.memberships} == {org_a.id, org_b.id}
