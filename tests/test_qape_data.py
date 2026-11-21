"""QAPE persistence contracts; SQLite unit tests, PostgreSQL migration tested separately."""
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.organization import Organization
from app.models.branch import Branch, Service
from app.models.user import User
from app.models.ticket import Ticket
from app.models.qape import AbusePolicy, AbuseEvent
from app.schemas.qape import PolicyConfig, PolicyInput, RestrictionInput, EventInput
from app.services.qape_data import PolicyRepository, RestrictionRepository, EventRepository, QapeScopeError


def config(timezone_name="Africa/Bujumbura"):
    return dict(limits=dict(bucket_capacity=2, bucket_refill_seconds=300,
                            max_requests_per_10m=3, max_tickets_per_day=3),
                risk=dict(review_threshold=2, cooldown_seconds=300, hard_block_seconds=900),
                counting=dict(timezone=timezone_name),
                staff_override=dict(enabled=True, require_reason=True))


async def world(db):
    org = Organization(name="Org", slug=uuid.uuid4().hex, sector="other")
    other = Organization(name="Other", slug=uuid.uuid4().hex, sector="other")
    db.add_all([org, other]); await db.flush()
    branch = Branch(name="Branch", slug=uuid.uuid4().hex, sector="other", org_id=org.id)
    db.add(branch); await db.flush()
    service = Service(name="S", org_id=org.id, branch_id=branch.id)
    user = User(phone="+257" + str(uuid.uuid4().int % 10**8).zfill(8))
    db.add_all([service, user]); await db.commit()
    return org, other, service, user, branch


@pytest.mark.asyncio
async def test_policy_inheritance_versions_and_activation(db_session):
    db = db_session
    org, other, service, user, _ = await world(db)
    repo = PolicyRepository(db)
    base = await repo.create(PolicyInput(org_id=org.id, version=1, enabled=True, config=config()))
    override = await repo.create(PolicyInput(org_id=org.id, service_id=service.id, version=1, enabled=True, config=config()))
    assert (await repo.effective(org.id, service.id)).id == override.id
    assert (await repo.effective(org.id, None)).id == base.id
    replacement = await repo.create(PolicyInput(org_id=org.id, service_id=service.id, version=2, enabled=False, config=config()))
    await repo.activate(org.id, replacement.id)
    assert (await repo.effective(org.id, service.id)).id == replacement.id
    assert [p.version for p in await repo.versions(org.id, service.id)] == [2, 1]
    with pytest.raises(QapeScopeError):
        await repo.create(PolicyInput(org_id=other.id, service_id=service.id, version=1, enabled=True, config=config()))


@pytest.mark.asyncio
async def test_policy_unique_active_and_versions(db_session):
    db = db_session
    org, _, service, _, _ = await world(db)
    repo = PolicyRepository(db)
    org_id = org.id
    await repo.create(PolicyInput(org_id=org_id, version=1, enabled=True, config=config()))
    await db.commit()
    with pytest.raises(IntegrityError):
        await repo.create(PolicyInput(org_id=org_id, version=2, enabled=True, config=config()))
    await db.rollback()
    with pytest.raises(IntegrityError):
        await repo.create(PolicyInput(org_id=org_id, version=1, enabled=False, config=config()))
    await db.rollback()


def test_invalid_config_and_sensitive_audit():
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate(config("not/a/timezone"))
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate(config() | {"counting": {"timezone": "Africa/Bujumbura", "count_cancelled_tickets": False}})
    with pytest.raises(ValidationError):
        EventInput(org_id="o", request_id="r", event_type="DENIED", reason_codes=[], metadata={"jwt": "secret"})
    with pytest.raises(ValidationError):
        EventInput(org_id="o", request_id="r", event_type="TICKET_CREATED", reason_codes=[])


@pytest.mark.asyncio
async def test_restriction_expiry_revocation_and_tenant(db_session):
    db = db_session
    org, other, service, user, _ = await world(db)
    repo = RestrictionRepository(db)
    now = datetime.now(timezone.utc)
    row = await repo.create(RestrictionInput(org_id=org.id, service_id=service.id, user_id=user.id,
        restriction_type="TEMPORARY_BLOCK", reason_code="RATE_LIMIT", starts_at=now-timedelta(seconds=30),
        expires_at=now+timedelta(minutes=1)))
    assert len(await repo.applicable(org.id, user.id, service.id, now)) == 1
    assert not await repo.applicable(org.id, user.id, service.id, now+timedelta(minutes=1))
    with pytest.raises(QapeScopeError):
        await repo.applicable(other.id, user.id, service.id, now)
    with pytest.raises(QapeScopeError):
        await repo.revoke(other.id, row.id, user.id)
    await repo.revoke(org.id, row.id, user.id)
    assert row.revoked_at is not None
    assert not await repo.applicable(org.id, user.id, service.id, now)


@pytest.mark.asyncio
async def test_events_ticket_uniqueness_isolation(db_session):
    db = db_session
    org, other, service, user, branch = await world(db)
    ticket = Ticket(user_id=user.id, org_id=org.id, agency_id=branch.id, service_id=service.id,
                    number="A-001", sequence=1, queue_sequence=1)
    db.add(ticket); await db.commit()
    repo = EventRepository(db)
    data = EventInput(org_id=org.id, service_id=service.id, user_id=user.id,
                      ticket_id=ticket.id, request_id="req-1", event_type="TICKET_CREATED",
                      decision="ALLOW", reason_codes=["OK"], metadata={})
    event = await repo.append(data)
    assert event.reason_codes == ["OK"] and event.provenance == "LIVE"
    assert [x.id for x in await repo.list(org.id)] == [event.id]
    now = datetime.now(timezone.utc)
    assert await repo.count_created(org.id, service.id, now-timedelta(days=1), now+timedelta(days=1)) == 1
    assert not await repo.list(other.id)
    other_id = other.id
    await db.commit()
    with pytest.raises(IntegrityError):
        await repo.append(data.model_copy(update={"request_id": "req-2"}))
    await db.rollback()
    with pytest.raises(QapeScopeError):
        await repo.append(data.model_copy(update={"org_id": other_id}))
