"""QAPE decision precedence and audit; SQLite for deterministic inputs."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
import uuid
import pytest
import pytest_asyncio
import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.core.redis import close_redis
from sqlalchemy import func, select
from app.models.qape import AbusePolicy, AbuseRestriction, AbuseEvent
from app.models.qape_operation import QapeCreateOperation
from app.services.qape_engine import QapeDecisionEngine, DecisionCode, SoftSignal, Decision
from tests.test_qape_data import world, config


@pytest_asyncio.fixture(autouse=True)
async def redis_lifecycle():
    yield
    await close_redis()


@pytest.mark.asyncio
async def test_restrictions_scope_expiry_revoke_and_soft_signals(db_session):
    db = db_session
    org, other, service, user, _ = await world(db)
    settings = config()
    settings['risk']['review_threshold'] = 2
    base = AbusePolicy(org_id=org.id, version=1, enabled=True, config=settings)
    override = AbusePolicy(org_id=org.id, service_id=service.id, version=1,
                           enabled=True, config=settings)
    db.add_all([base, override]); await db.commit()
    engine = QapeDecisionEngine(db)
    assert (await engine.effective_policy(org.id, service.id)).id == override.id
    now = datetime.now(timezone.utc)
    args = dict(policy=override, org_id=org.id, service_id=service.id, user_id=user.id, now=now)
    assert (await engine.preflight(**args)).code == DecisionCode.ALLOW
    signals = (SoftSignal('A', 'verified'), SoftSignal('A', 'second-source'))
    assert (await engine.preflight(**args, signals=signals)).code == DecisionCode.ALLOW
    assert (await engine.preflight(**args, signals=signals + (SoftSignal('B', 'verified'),))).code == DecisionCode.REVIEW_REQUIRED
    rows = [
        AbuseRestriction(org_id=org.id, service_id=service.id, user_id=user.id,
            restriction_type='TEMPORARY_BLOCK', reason_code='EXPLICIT_RESTRICTION',
            starts_at=now-timedelta(minutes=1), expires_at=now+timedelta(minutes=5)),
        AbuseRestriction(org_id=org.id, user_id=user.id, restriction_type='TEMPORARY_BLOCK',
            reason_code='EXPIRED', starts_at=now-timedelta(hours=2),
            expires_at=now-timedelta(hours=1)),
        AbuseRestriction(org_id=org.id, user_id=user.id, restriction_type='TEMPORARY_BLOCK',
            reason_code='REVOKED', starts_at=now-timedelta(minutes=1),
            expires_at=now+timedelta(minutes=1), revoked_at=now),
        AbuseRestriction(org_id=other.id, user_id=user.id, restriction_type='TEMPORARY_BLOCK',
            reason_code='OTHER_TENANT', starts_at=now-timedelta(minutes=1),
            expires_at=now+timedelta(minutes=1)),
    ]
    db.add_all(rows); await db.commit()
    decision = await engine.preflight(**args, signals=signals + (SoftSignal('B', 'verified'),))
    assert decision.code == DecisionCode.TEMPORARY_BLOCK
    assert 'EXPLICIT_RESTRICTION' in decision.reason_codes
    assert 'EXPIRED' not in decision.reason_codes and 'OTHER_TENANT' not in decision.reason_codes
    assert decision.retry_after == rows[0].expires_at
    rows[0].revoked_at = now; await db.commit()
    assert (await engine.preflight(**args)).code == DecisionCode.ALLOW
    with pytest.raises(ValueError):
        await engine.preflight(policy=override, org_id=other.id,
                               service_id=service.id, user_id=user.id, now=now)


@pytest.mark.asyncio
async def test_redis_down_modes_and_audit_once(db_session):
    db = db_session
    org, _, svc, user, branch = await world(db)
    org_id, service_id, user_id, agency_id = org.id, svc.id, user.id, branch.id
    engine = QapeDecisionEngine(db)
    for mode, expected in [('ALLOW', DecisionCode.ALLOW),
                           ('REVIEW_REQUIRED', DecisionCode.REVIEW_REQUIRED),
                           ('TEMPORARY_BLOCK', DecisionCode.TEMPORARY_BLOCK)]:
        cfg = config() | {'redis_unavailable': mode}
        cfg['risk']['hard_block_seconds'] = 37
        cfg['risk']['cooldown_seconds'] = 5
        policy = AbusePolicy(org_id=org_id, service_id=service_id, version=1,
                             enabled=True, config=cfg)
        with patch('app.core.redis.get_redis', new_callable=AsyncMock, side_effect=ConnectionError):
            decision = await engine.admit(policy=policy, org_id=org_id,
                service_id=service_id, user_id=user_id, reservation_id=str(uuid.uuid4()),
                attempt_generation=0)
        assert decision.code == expected and not decision.redis_reserved
        if mode == 'TEMPORARY_BLOCK':
            remaining = (decision.retry_after - datetime.now(timezone.utc)).total_seconds()
            assert 35 <= remaining <= 37
        op = QapeCreateOperation(org_id=org_id, agency_id=agency_id, service_id=service_id,
            user_id=user_id, client_action_id='audit_' + uuid.uuid4().hex,
            operation_type='CREATE_TICKET', payload_hash='f'*64, attempt_generation=0)
        db.add(op); await db.flush()
        await engine.record(op, decision)
        await engine.record(op, decision)
        count = await db.scalar(select(func.count(AbuseEvent.id)).where(
            AbuseEvent.details['operation_id'].as_string() == op.id))
        assert count == 1
        assert await db.scalar(select(func.count(AbuseEvent.id)).where(
            AbuseEvent.details['operation_id'].as_string() == op.id,
            AbuseEvent.event_type == 'TICKET_CREATED')) == 0
        await db.rollback()  # clear the uncommitted synthetic operation/event


@pytest.mark.asyncio
async def test_real_postgres_and_redis_decision_priority():
    if not os.getenv('PG_TEST_URL') or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('requires disposable PostgreSQL 15 + Redis 7')
    engine_db = create_async_engine(os.environ['PG_TEST_URL'])
    async with async_sessionmaker(engine_db, expire_on_commit=False)() as db:
        org, _, svc, user, _ = await world(db)
        cfg = config()
        cfg['limits']['max_requests_per_10m'] = 100
        cfg['limits']['bucket_capacity'] = 1
        cfg['limits']['bucket_refill_seconds'] = 5
        cfg['risk']['hard_block_seconds'] = 37
        cfg['risk']['cooldown_seconds'] = 5
        policy = AbusePolicy(org_id=org.id, service_id=svc.id, version=1,
                             enabled=True, config=cfg)
        db.add(policy); await db.commit()
        decision_engine = QapeDecisionEngine(db)
        assert (await decision_engine.preflight(policy=policy, org_id=org.id,
                    service_id=svc.id, user_id=user.id)).code == DecisionCode.ALLOW
        base = dict(policy=policy, org_id=org.id, service_id=svc.id, user_id=user.id)
        one = await decision_engine.admit(**base, reservation_id=str(uuid.uuid4()), attempt_generation=0)
        two = await decision_engine.admit(**base, reservation_id=str(uuid.uuid4()), attempt_generation=0)
        assert one.code == DecisionCode.ALLOW and one.redis_reserved
        assert two.code == DecisionCode.TEMPORARY_BLOCK
        assert two.reason_codes == ('RATE_LIMITED',) and two.retry_after
        ordinary_delay = (two.retry_after - datetime.now(timezone.utc)).total_seconds()
        assert 4 <= ordinary_delay <= 6  # cooldown=5, NOT hard_block=37
        # A longer technical sliding-window wait must still win over cooldown.
        from app.models.user import User
        window_user = User(phone='+257' + str(uuid.uuid4().int % 10**8).zfill(8))
        db.add(window_user); await db.flush()
        window_cfg = config()
        window_cfg['limits']['bucket_capacity'] = 10
        window_cfg['limits']['max_requests_per_10m'] = 1
        window_cfg['risk']['cooldown_seconds'] = 5
        window_cfg['risk']['hard_block_seconds'] = 37
        window_policy = AbusePolicy(org_id=org.id, service_id=svc.id,
            config=window_cfg, version=2, enabled=False)
        window_base = dict(policy=window_policy, org_id=org.id, service_id=svc.id,
                           user_id=window_user.id)
        assert (await decision_engine.admit(**window_base,
            reservation_id=str(uuid.uuid4()), attempt_generation=0)).code == DecisionCode.ALLOW
        window_denial = await decision_engine.admit(**window_base,
            reservation_id=str(uuid.uuid4()), attempt_generation=0)
        assert window_denial.code == DecisionCode.TEMPORARY_BLOCK
        assert (window_denial.retry_after - datetime.now(timezone.utc)).total_seconds() > 500
    await engine_db.dispose()

@pytest.mark.asyncio
async def test_business_day_original_service_and_provenance(db_session):
    """Quota counts creation evidence, not mutable ticket status/service."""
    from app.models.branch import Service
    from app.models.ticket import Ticket, TicketStatus
    db = db_session
    org, _, svc, user, branch = await world(db)
    target = Service(name='Target', org_id=org.id, branch_id=branch.id)
    db.add(target); await db.flush()
    cfg = config('Africa/Bujumbura')
    cfg['limits']['max_tickets_per_day'] = 1
    policy = AbusePolicy(org_id=org.id, service_id=svc.id, version=1,
                         enabled=True, config=cfg)
    db.add(policy)
    # Jan 1 22:30 UTC belongs to Jan 2 in the institution's business timezone.
    created = datetime(2026, 1, 1, 22, 30, tzinfo=timezone.utc)
    ticket = Ticket(org_id=org.id, agency_id=branch.id, service_id=svc.id,
                    user_id=user.id, number='QAPE-DAY-1', sequence=1,
                    queue_sequence=1, status=TicketStatus.DONE, created_at=created)
    db.add(ticket); await db.flush()
    db.add(AbuseEvent(org_id=org.id, service_id=svc.id, user_id=user.id,
        ticket_id=ticket.id, request_id=uuid.uuid4().hex, event_type='TICKET_CREATED',
        provenance='LIVE', decision='ALLOW', reason_codes=[], policy_version=1,
        created_at=created, details={'origin_service_id': svc.id}))
    inferred_ticket = Ticket(org_id=org.id, agency_id=branch.id, service_id=svc.id,
        user_id=user.id, number='QAPE-DAY-2', sequence=2, queue_sequence=2,
        status=TicketStatus.CANCELLED, created_at=created-timedelta(minutes=10))
    db.add(inferred_ticket); await db.flush()
    db.add(AbuseEvent(org_id=org.id, service_id=svc.id, user_id=user.id,
        ticket_id=inferred_ticket.id, request_id=uuid.uuid4().hex,
        event_type='TICKET_CREATED', provenance='HISTORICAL_INFERRED',
        decision=None, reason_codes=[], created_at=created-timedelta(minutes=10), details={}))
    await db.commit()
    org_id, source_id, target_id, user_id = org.id, svc.id, target.id, user.id
    engine = QapeDecisionEngine(db)
    from app.services.qape_data import EventRepository
    assert await EventRepository(db).count_created(org_id, source_id,
        datetime(2026, 1, 1, 22, tzinfo=timezone.utc),
        datetime(2026, 1, 2, 22, tzinfo=timezone.utc), user_id=user_id) == 1
    assert (await engine.preflight(policy=policy, org_id=org_id, service_id=source_id,
        user_id=user_id, now=datetime(2026, 1, 1, 21, 30, tzinfo=timezone.utc))).code == DecisionCode.ALLOW
    blocked = await engine.preflight(policy=policy, org_id=org_id, service_id=source_id,
        user_id=user_id, now=datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc))
    assert blocked.code == DecisionCode.TEMPORARY_BLOCK
    assert blocked.reason_codes == ('DAILY_QUOTA_REACHED',)
    assert blocked.retry_after == datetime(2026, 1, 2, 22, 0, tzinfo=timezone.utc)
    ticket.service_id = target_id  # transfer changes ticket, never creation evidence
    ticket.status = TicketStatus.CANCELLED
    await db.commit()
    assert (await engine.preflight(policy=policy, org_id=org_id, service_id=source_id,
        user_id=user_id, now=datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc))).code == DecisionCode.TEMPORARY_BLOCK
    target_policy = AbusePolicy(org_id=org_id, service_id=target_id, version=1,
                                enabled=True, config=cfg)
    assert (await engine.preflight(policy=target_policy, org_id=org_id, service_id=target_id,
        user_id=user_id, now=datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc))).code == DecisionCode.ALLOW
    assert (await engine.preflight(policy=policy, org_id=org_id, service_id=source_id,
        user_id=user_id, now=datetime(2026, 1, 2, 22, 0, tzinfo=timezone.utc))).code == DecisionCode.ALLOW
