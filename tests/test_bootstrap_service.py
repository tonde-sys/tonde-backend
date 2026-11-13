"""Tests du bootstrap idempotent d'une organisation pilote."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import verify_password
from app.models.branch import Branch, Service
from app.models.counter import Counter
from app.models.employee import Employee, EmployeeStatus
from app.models.organization import Organization
from app.models.user import User, UserRole
from app.models.user_organization import UserOrganization
from app.schemas.auth import LoginEmailRequest
from app.services.auth_service import AuthService
from app.services.bootstrap_service import BootstrapError, bootstrap_pilot


def _config(**overrides):
    config = {
        "organization": {
            "name": "Banque Pilote",
            "slug": "banque-pilote",
            "sector": "bank",
            "country": "Burundi",
            "city": "Bujumbura",
            "email": "contact@banque-pilote.bi",
        },
        "branch": {
            "name": "Agence Centre",
            "slug": "banque-pilote-centre",
            "city": "Bujumbura",
            "address": "Avenue de la Banque",
            "opens_at": "08:00",
            "closes_at": "17:00",
            "is_open": True,
        },
        "services": [
            {"name": "Caisse", "ticket_prefix": "A", "avg_duration_minutes": 5},
            {"name": "Crédit", "ticket_prefix": "B", "avg_duration_minutes": 12},
        ],
        "counters": [{"name": "Guichet 1"}, {"name": "Guichet 2"}],
        "users": [
            {
                "name": "Super Admin",
                "email": "super@banque-pilote.bi",
                "role": "super_admin",
            },
            {
                "name": "Admin Organisation",
                "email": "admin@banque-pilote.bi",
                "role": "admin_org",
            },
            {
                "name": "Agent Un",
                "email": "agent1@banque-pilote.bi",
                "role": "agent",
                "counter": "Guichet 1",
            },
            {
                "name": "Agent Deux",
                "email": "agent2@banque-pilote.bi",
                "role": "agent",
                "counter": "Guichet 2",
            },
        ],
    }
    config.update(overrides)
    return config


async def _count(db_session, model) -> int:
    return int(await db_session.scalar(select(func.count(model.id))))


async def test_bootstrap_creates_complete_pilot_organization(db_session):
    passwords = {
        "super@banque-pilote.bi": "SuperPass123",
        "admin@banque-pilote.bi": "AdminPass123",
        "agent1@banque-pilote.bi": "AgentOne123",
        "agent2@banque-pilote.bi": "AgentTwo123",
    }
    result = await bootstrap_pilot(db_session, _config(), passwords=passwords)

    assert result.dry_run is False
    assert result.organization["slug"] == "banque-pilote"
    assert result.branch["slug"] == "banque-pilote-centre"
    assert len(result.services) == 2
    assert len(result.counters) == 2
    assert len(result.users) == 4
    assert result.generated_passwords == {}
    assert result.created == {
        "organizations": 1,
        "branches": 1,
        "services": 2,
        "counters": 2,
        "users": 4,
        "memberships": 3,
        "employees": 3,
    }

    assert await _count(db_session, Organization) == 1
    assert await _count(db_session, Branch) == 1
    assert await _count(db_session, Service) == 2
    assert await _count(db_session, Counter) == 2
    assert await _count(db_session, User) == 4
    assert await _count(db_session, UserOrganization) == 3
    assert await _count(db_session, Employee) == 3

    org = await db_session.scalar(select(Organization).where(Organization.slug == "banque-pilote"))
    assert org is not None

    super_admin = await db_session.scalar(select(User).where(User.email == "super@banque-pilote.bi"))
    assert super_admin is not None
    assert super_admin.role == UserRole.SUPER_ADMIN
    assert super_admin.org_id is None
    assert super_admin.hashed_password is not None
    assert verify_password("SuperPass123", super_admin.hashed_password)

    admin = await db_session.scalar(select(User).where(User.email == "admin@banque-pilote.bi"))
    assert admin is not None
    assert admin.role == UserRole.ADMIN_ORG
    assert admin.org_id == org.id
    assert admin.is_active and admin.is_verified
    assert verify_password("AdminPass123", admin.hashed_password)

    membership = await db_session.scalar(
        select(UserOrganization).where(
            UserOrganization.user_id == admin.id,
            UserOrganization.organization_id == org.id,
        )
    )
    assert membership is not None
    assert membership.status == "active"

    employee = await db_session.scalar(
        select(Employee).where(Employee.user_id == admin.id, Employee.org_id == org.id)
    )
    assert employee is not None
    assert employee.role == UserRole.ADMIN_ORG
    assert employee.status == EmployeeStatus.ACTIVE
    assert employee.agency_id == result.branch["id"]
    assert employee.counter_id is None

    counter = await db_session.scalar(select(Counter).where(Counter.name == "Guichet 1"))
    assert counter is not None
    agent = await db_session.scalar(select(User).where(User.email == "agent1@banque-pilote.bi"))
    assert agent is not None
    agent_employee = await db_session.scalar(
        select(Employee).where(Employee.user_id == agent.id, Employee.org_id == org.id)
    )
    assert agent_employee is not None
    assert agent_employee.counter_id == counter.id
    assert agent_employee.role == UserRole.AGENT


async def test_bootstrap_is_idempotent(db_session):
    config = _config()
    passwords = {"admin@banque-pilote.bi": "AdminPass123"}

    first = await bootstrap_pilot(db_session, config, passwords=passwords)
    counts_before = {
        "organizations": await _count(db_session, Organization),
        "branches": await _count(db_session, Branch),
        "services": await _count(db_session, Service),
        "counters": await _count(db_session, Counter),
        "users": await _count(db_session, User),
        "memberships": await _count(db_session, UserOrganization),
        "employees": await _count(db_session, Employee),
    }

    second = await bootstrap_pilot(db_session, config, passwords=passwords)

    assert second.created == {}
    assert all(user["created"] is False for user in second.users)
    assert second.generated_passwords == {}
    assert second.organization["id"] == first.organization["id"]
    assert second.branch["id"] == first.branch["id"]
    assert counts_before == {
        "organizations": await _count(db_session, Organization),
        "branches": await _count(db_session, Branch),
        "services": await _count(db_session, Service),
        "counters": await _count(db_session, Counter),
        "users": await _count(db_session, User),
        "memberships": await _count(db_session, UserOrganization),
        "employees": await _count(db_session, Employee),
    }


async def test_bootstrap_generates_password_only_for_new_accounts(db_session):
    config = _config()
    first = await bootstrap_pilot(db_session, config)

    assert set(first.generated_passwords) == {
        "super@banque-pilote.bi",
        "admin@banque-pilote.bi",
        "agent1@banque-pilote.bi",
        "agent2@banque-pilote.bi",
    }
    for email, password in first.generated_passwords.items():
        user = await db_session.scalar(select(User).where(User.email == email))
        assert user is not None
        assert user.hashed_password is not None
        assert verify_password(password, user.hashed_password)

    second = await bootstrap_pilot(db_session, config)
    assert second.generated_passwords == {}


async def test_bootstrap_reset_passwords_is_explicit(db_session):
    config = _config()
    await bootstrap_pilot(
        db_session,
        config,
        passwords={"admin@banque-pilote.bi": "OldPassword123"},
    )
    admin = await db_session.scalar(select(User).where(User.email == "admin@banque-pilote.bi"))
    assert admin is not None
    assert verify_password("OldPassword123", admin.hashed_password)

    # Un simple rerun ne change pas le mot de passe.
    await bootstrap_pilot(
        db_session,
        config,
        passwords={"admin@banque-pilote.bi": "IgnoredPassword123"},
    )
    await db_session.refresh(admin)
    assert verify_password("OldPassword123", admin.hashed_password)

    # La réinitialisation doit être demandée explicitement.
    await bootstrap_pilot(
        db_session,
        config,
        passwords={"admin@banque-pilote.bi": "NewPassword123"},
        reset_passwords=True,
    )
    await db_session.refresh(admin)
    assert verify_password("NewPassword123", admin.hashed_password)
    assert not verify_password("OldPassword123", admin.hashed_password)


async def test_bootstrap_rejects_duplicate_ticket_prefix_and_rolls_back(db_session):
    config = _config(
        services=[
            {"name": "Caisse", "ticket_prefix": "A"},
            {"name": "Crédit", "ticket_prefix": "A"},
        ]
    )
    with pytest.raises(BootstrapError, match="préfixe"):
        await bootstrap_pilot(db_session, config)

    assert await _count(db_session, Organization) == 0
    assert await _count(db_session, Branch) == 0


async def test_bootstrap_adds_service_to_existing_organization(db_session):
    first = await bootstrap_pilot(db_session, _config())
    config = _config(
        services=[
            {"name": "Caisse", "ticket_prefix": "A", "avg_duration_minutes": 5},
            {"name": "Crédit", "ticket_prefix": "B", "avg_duration_minutes": 12},
            {"name": "Ouverture de compte", "ticket_prefix": "C", "avg_duration_minutes": 20},
        ]
    )
    second = await bootstrap_pilot(db_session, config)

    assert second.organization["id"] == first.organization["id"]
    assert second.created == {"services": 1}
    assert await _count(db_session, Service) == 3
    assert await _count(db_session, Organization) == 1


async def test_bootstrap_dry_run_writes_nothing(db_session):
    result = await bootstrap_pilot(db_session, _config(), dry_run=True)

    assert result.dry_run is True
    assert result.organization["id"]
    assert await _count(db_session, Organization) == 0
    assert await _count(db_session, User) == 0


async def test_bootstrapped_admin_can_login(db_session):
    await bootstrap_pilot(
        db_session,
        _config(),
        passwords={"admin@banque-pilote.bi": "AdminPass123"},
    )
    auth = await AuthService(db_session).login_email(
        LoginEmailRequest(email="admin@banque-pilote.bi", password="AdminPass123")
    )

    assert auth.access_token
    assert auth.refresh_token
    assert auth.user.email == "admin@banque-pilote.bi"
    assert auth.user.role == UserRole.ADMIN_ORG.value


async def test_cli_bootstrap_dry_run_reads_config_file(
    db_engine, db_session, monkeypatch, tmp_path
):
    from app import cli

    config_path = tmp_path / "pilot.json"
    config_path.write_text(json.dumps(_config()), encoding="utf-8")

    session_factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(cli, "AsyncSessionLocal", session_factory)

    args = cli._build_parser().parse_args(
        ["bootstrap", "--config", str(config_path), "--dry-run"]
    )
    exit_code = await cli._run_bootstrap(args)

    assert exit_code == 0
    assert await _count(db_session, Organization) == 0
    assert await _count(db_session, User) == 0


async def test_bootstrap_uses_password_env(db_session, monkeypatch):
    monkeypatch.setenv("PILOT_ADMIN_PASSWORD", "EnvPassword123")
    config = _config()
    config["users"] = [
        {
            "name": "Admin Organisation",
            "email": "admin@banque-pilote.bi",
            "role": "admin_org",
            "password_env": "PILOT_ADMIN_PASSWORD",
        }
    ]

    result = await bootstrap_pilot(db_session, config)

    assert result.generated_passwords == {}
    admin = await db_session.scalar(select(User).where(User.email == "admin@banque-pilote.bi"))
    assert admin is not None
    assert admin.hashed_password is not None
    assert verify_password("EnvPassword123", admin.hashed_password)

    auth = await AuthService(db_session).login_email(
        LoginEmailRequest(email="admin@banque-pilote.bi", password="EnvPassword123")
    )
    assert auth.access_token
