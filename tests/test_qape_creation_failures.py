"""Failure paths on disposable PostgreSQL 15 and Redis 7."""
import os
import uuid
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.core.redis import close_redis
from app.models.organization import Organization
from app.models.branch import Branch, Service
from app.models.user import User
from app.models.ticket import Ticket, TicketStatus
from app.models.qape import AbusePolicy, AbuseEvent
from app.models.qape_operation import QapeCreateOperation, OperationState
from app.schemas.ticket import CreateTicketRequest
from app.services.ticket_service import TicketService
from app.services.qape_recovery import claim_expired, reconcile_one
from app.services.qape_reservation import reserve, reservation_state
from tests.test_qape_data import config


@pytest_asyncio.fixture(autouse=True)
async def redis_lifecycle():
    yield
    await close_redis()


@pytest.mark.asyncio
async def test_redis_unknown_and_fenced_compensation():
    if not os.getenv('PG_TEST_URL') or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('requires disposable PostgreSQL + real Redis')
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        org = Organization(name='QAPE fail test', slug=uuid.uuid4().hex, sector='other')
        db.add(org); await db.flush()
        branch = Branch(name='test', slug=uuid.uuid4().hex, sector='other', org_id=org.id,
                        is_open=True, opens_at='00:00', closes_at='23:59')
        db.add(branch); await db.flush()
        svc = Service(name='test', org_id=org.id, branch_id=branch.id)
        user = User(phone='+257' + str(uuid.uuid4().int % 10**8).zfill(8))
        db.add_all([svc, user]); await db.flush()
        db.add(AbusePolicy(org_id=org.id, service_id=svc.id, version=1, enabled=True, config=config()))
        await db.commit()
        data = CreateTicketRequest(agency_id=branch.id, service_id=svc.id,
                                   client_action_id='failure_' + uuid.uuid4().hex)
        with patch('app.services.qape_engine.reserve', new_callable=AsyncMock, side_effect=TimeoutError):
            with pytest.raises(HTTPException) as err:
                await TicketService(db).create_ticket(data, user.id, None)
        assert err.value.status_code == 503
        op = (await db.scalars(select(QapeCreateOperation).where(QapeCreateOperation.client_action_id == data.client_action_id))).one()
        assert op.state == OperationState.RECOVERY_REQUIRED.value
        assert await db.scalar(select(func.count(Ticket.id)).where(Ticket.user_id == user.id)) == 0
        with pytest.raises(HTTPException) as retry:
            await TicketService(db).create_ticket(data, user.id, None)
        assert retry.value.detail['code'] == 'RECOVERY_REQUIRED'
        # Real recovery sees missing Redis reservation: never blind-reimburses.
        op.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await db.commit()
        claims = await claim_expired(db, owner='worker-a')
        assert (op.id, op.lease_generation) in claims
        result = await reconcile_one(db, op.id, 'worker-a', op.lease_generation)
        assert result == 'RECOVERY_REQUIRED'
        assert op.decision_code == 'RESERVATION_UNKNOWN'
    await engine.dispose()


@pytest.mark.asyncio
async def test_preflight_redis_outage_requires_review():
    if not os.getenv('PG_TEST_URL') or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('requires disposable PostgreSQL + real Redis')
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        org = Organization(name='QAPE review test', slug=uuid.uuid4().hex, sector='other')
        db.add(org); await db.flush()
        branch = Branch(name='test', slug=uuid.uuid4().hex, sector='other', org_id=org.id,
                        is_open=True, opens_at='00:00', closes_at='23:59')
        db.add(branch); await db.flush()
        svc = Service(name='test', org_id=org.id, branch_id=branch.id)
        user = User(phone='+257' + str(uuid.uuid4().int % 10**8).zfill(8))
        db.add_all([svc, user]); await db.flush()
        db.add(AbusePolicy(org_id=org.id, service_id=svc.id, version=1, enabled=True, config=config()))
        await db.commit()
        data = CreateTicketRequest(agency_id=branch.id, service_id=svc.id,
                                   client_action_id='review_' + uuid.uuid4().hex)
        with patch('app.core.redis.get_redis', new_callable=AsyncMock, side_effect=ConnectionError):
            with pytest.raises(HTTPException) as err:
                await TicketService(db).create_ticket(data, user.id, None)
        assert err.value.status_code == 202 and err.value.detail['code'] == 'REVIEW_REQUIRED'
        op = (await db.scalars(select(QapeCreateOperation).where(QapeCreateOperation.client_action_id == data.client_action_id))).one()
        assert op.state == OperationState.REVIEW_REQUIRED.value
        assert await db.scalar(select(func.count(Ticket.id)).where(Ticket.user_id == user.id)) == 0
        with pytest.raises(HTTPException) as retry:
            await TicketService(db).create_ticket(data, user.id, None)
        assert retry.value.detail == err.value.detail
        # Disabling the policy must not route this key through legacy creation.
        policy = (await db.scalars(select(AbusePolicy).where(AbusePolicy.service_id == svc.id))).one()
        policy.enabled = False
        await db.commit()
        with pytest.raises(HTTPException) as disabled_retry:
            await TicketService(db).create_ticket(data, user.id, None)
        assert disabled_retry.value.status_code == 202
        assert disabled_retry.value.detail == err.value.detail
        assert await db.scalar(select(func.count(Ticket.id)).where(Ticket.user_id == user.id)) == 0
        with pytest.raises(HTTPException) as changed:
            await TicketService(db).create_ticket(data.model_copy(update={'priority':'vip'}), user.id, None)
        assert changed.value.detail['code'] == 'IDEMPOTENCY_PAYLOAD_CONFLICT'
        # Explicit service override may bypass ONLY the QAPE Redis limit;
        # the independent TONDE checks and SQL uniqueness still run.
        policy.enabled = True
        policy.config = {**policy.config, 'redis_unavailable': 'ALLOW'}
        await db.commit()
        other = User(phone='+257' + str(uuid.uuid4().int % 10**8).zfill(8))
        db.add(other); await db.commit()
        bypass_data = data.model_copy(update={'client_action_id': 'bypass_' + uuid.uuid4().hex})
        with patch('app.core.redis.get_redis', new_callable=AsyncMock, side_effect=ConnectionError):
            bypass = await TicketService(db).create_ticket(bypass_data, other.id, None)
        assert bypass.id
        bypass_op = (await db.scalars(select(QapeCreateOperation).where(
            QapeCreateOperation.client_action_id == bypass_data.client_action_id))).one()
        assert bypass_op.state == OperationState.COMMITTED.value
        assert bypass_op.decision_code == 'REDIS_UNAVAILABLE_BYPASS'
        from app.models.qape import AbuseEvent
        event = (await db.scalars(select(AbuseEvent).where(AbuseEvent.ticket_id == bypass.id))).one()
        assert event.decision == 'ALLOW' and event.provenance == 'LIVE'
        assert event.reason_codes == ['REDIS_UNAVAILABLE_BYPASS']
        # The append-only audit repository can retrieve this event by scope.
        from app.services.qape_data import EventRepository
        assert any(e.id == event.id and 'REDIS_UNAVAILABLE_BYPASS' in e.reason_codes
                   for e in await EventRepository(db).list(org.id, service_id=svc.id))
    await engine.dispose()


@pytest.mark.asyncio
async def test_outbox_failure_rolls_back_ticket_event_and_log():
    if not os.getenv('PG_TEST_URL') or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('requires disposable PostgreSQL + real Redis')
    from app.models.qape import AbuseEvent
    from app.models.queue_log import QueueLog
    from app.models.event_outbox import EventOutbox
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        org = Organization(name='QAPE rollback test', slug=uuid.uuid4().hex, sector='other')
        db.add(org); await db.flush()
        branch = Branch(name='test', slug=uuid.uuid4().hex, sector='other', org_id=org.id,
                        is_open=True, opens_at='00:00', closes_at='23:59')
        db.add(branch); await db.flush()
        svc = Service(name='test', org_id=org.id, branch_id=branch.id)
        user = User(phone='+257' + str(uuid.uuid4().int % 10**8).zfill(8))
        db.add_all([svc, user]); await db.flush()
        db.add(AbusePolicy(org_id=org.id, service_id=svc.id, version=1, enabled=True, config=config()))
        await db.commit()
        data = CreateTicketRequest(agency_id=branch.id, service_id=svc.id,
                                   client_action_id='rollback_' + uuid.uuid4().hex)
        user_id, org_id = user.id, org.id
        with patch('app.services.ticket_service.OutboxService.enqueue', new_callable=AsyncMock,
                   side_effect=RuntimeError('synthetic SQL failure before commit')):
            with pytest.raises(RuntimeError):
                await TicketService(db).create_ticket(data, user.id, None)
        await db.rollback()
        op = (await db.scalars(select(QapeCreateOperation).where(QapeCreateOperation.client_action_id == data.client_action_id))).one()
        assert op.state == OperationState.RESERVED.value
        assert await db.scalar(select(func.count(Ticket.id)).where(Ticket.user_id == user_id)) == 0
        assert await db.scalar(select(func.count(AbuseEvent.id)).where(AbuseEvent.user_id == user_id)) == 0
        assert await db.scalar(select(func.count(QueueLog.id)).where(QueueLog.org_id == org_id)) == 0
        assert await db.scalar(select(func.count(EventOutbox.id)).where(EventOutbox.org_id == org_id)) == 0
        op.lease_expires_at = datetime.now(timezone.utc)-timedelta(seconds=1)
        await db.commit()
        claim = await claim_expired(db, owner='rollback-worker', operation_id=op.id)
        assert claim == [(op.id, op.lease_generation)]
        assert await reconcile_one(db, op.id, 'rollback-worker', op.lease_generation) == 'COMPENSATED'
    await engine.dispose()


@pytest.mark.asyncio
async def test_policy_daily_limit_is_serialized_and_counts_live():
    if not os.getenv('PG_TEST_URL') or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('requires disposable PostgreSQL + real Redis')
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        org = Organization(name='QAPE daily test', slug=uuid.uuid4().hex, sector='other')
        db.add(org); await db.flush()
        branch = Branch(name='test', slug=uuid.uuid4().hex, sector='other', org_id=org.id,
                        is_open=True, opens_at='00:00', closes_at='23:59')
        db.add(branch); await db.flush()
        svc = Service(name='test', org_id=org.id, branch_id=branch.id)
        users = [User(phone='+257' + str(uuid.uuid4().int % 10**8).zfill(8)) for _ in range(2)]
        db.add_all([svc, *users]); await db.flush()
        limits = config()
        limits['limits']['max_tickets_per_day'] = 1
        db.add(AbusePolicy(org_id=org.id, service_id=svc.id, version=1, enabled=True, config=limits))
        await db.commit()
        first_user, other_user = [user.id for user in users]
        first = await TicketService(db).create_ticket(CreateTicketRequest(
            agency_id=branch.id, service_id=svc.id,
            client_action_id='daily_' + uuid.uuid4().hex), first_user, None)
        assert first.id
        # The quota belongs to a user AND a service, not to all users sharing it.
        assert (await TicketService(db).create_ticket(CreateTicketRequest(
            agency_id=branch.id, service_id=svc.id,
            client_action_id='daily_' + uuid.uuid4().hex), other_user, None)).id
        ticket = await db.get(Ticket, first.id)
        ticket.status = TicketStatus.DONE
        await db.commit()
        with pytest.raises(HTTPException) as error:
            await TicketService(db).create_ticket(CreateTicketRequest(
                agency_id=branch.id, service_id=svc.id,
                client_action_id='daily_' + uuid.uuid4().hex), first_user, None)
        assert error.value.status_code == 429
        assert error.value.detail['code'] == 'DAILY_QUOTA_REACHED'
        assert await db.scalar(select(func.count(Ticket.id)).where(Ticket.user_id == first_user)) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_initial_commit_interruption_without_reservation():
    if not os.getenv('PG_TEST_URL'):
        pytest.skip('requires disposable PostgreSQL')
    from tests.test_qape_data import world
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        org, _, svc, user, branch = await world(db)
        now = datetime.now(timezone.utc)
        rows = [
            QapeCreateOperation(org_id=org.id, agency_id=branch.id, service_id=svc.id,
                user_id=user.id, operation_type='CREATE_TICKET', client_action_id='stage_' + uuid.uuid4().hex,
                payload_hash='a'*64, state=OperationState.PENDING.value,
                created_at=now-timedelta(minutes=2), lease_generation=0, attempt_generation=0),
            QapeCreateOperation(org_id=org.id, agency_id=branch.id, service_id=svc.id,
                user_id=user.id, operation_type='CREATE_TICKET', client_action_id='stage_' + uuid.uuid4().hex,
                payload_hash='a'*64, state=OperationState.PENDING.value, lease_owner='dead-writer',
                lease_expires_at=now-timedelta(seconds=5), lease_generation=0, attempt_generation=0),
            QapeCreateOperation(org_id=org.id, agency_id=branch.id, service_id=svc.id,
                user_id=user.id, operation_type='CREATE_TICKET', client_action_id='stage_' + uuid.uuid4().hex,
                payload_hash='a'*64, state=OperationState.RESERVED.value, lease_owner='dead-writer',
                lease_expires_at=now-timedelta(seconds=5), lease_generation=0, attempt_generation=0,
                decision_code='REDIS_UNAVAILABLE_BYPASS'),
        ]
        db.add_all(rows); await db.commit()
        ids = [row.id for row in rows]
        for index, op_id in enumerate(ids):
            owner = 'initial-worker-' + str(index)
            claim = await claim_expired(db, owner=owner, operation_id=op_id)
            assert len(claim) == 1
            result = await reconcile_one(db, op_id, owner, claim[0][1])
            assert result == ('COMPENSATED' if index == 2 else 'PENDING')
            row = await db.get(QapeCreateOperation, op_id)
            if index != 2:
                assert row.lease_owner is None and row.reservation_id is None
                assert row.decision_code == 'READY_TO_RETRY'
    await engine.dispose()


@pytest.mark.asyncio
async def test_commit_ack_lost_replays_durable_result():
    if not os.getenv('PG_TEST_URL') or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('requires disposable PostgreSQL + real Redis')
    from tests.test_qape_data import world
    from app.models.qape import AbuseEvent
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        org, _, svc, user, branch = await world(db)
        branch.is_open = True
        branch.opens_at, branch.closes_at = '00:00', '23:59'
        db.add(AbusePolicy(org_id=org.id, service_id=svc.id, version=1, enabled=True, config=config()))
        await db.commit()
        data = CreateTicketRequest(agency_id=branch.id, service_id=svc.id,
                                   client_action_id='lostack_' + uuid.uuid4().hex)
        original_commit = db.commit
        count = 0
        async def commit_then_lose_ack():
            nonlocal count
            count += 1
            await original_commit()
            if count == 4:  # intent, reservation identity, RESERVED, final business transaction
                raise ConnectionError('simulated lost PostgreSQL COMMIT acknowledgement')
        with patch.object(db, 'commit', side_effect=commit_then_lose_ack):
            with pytest.raises(ConnectionError):
                await TicketService(db).create_ticket(data, user.id, None)
        committed = await TicketService(db).create_ticket(data, user.id, None)
        assert committed.id
        assert await db.scalar(select(func.count(Ticket.id)).where(Ticket.user_id == user.id)) == 1
        assert await db.scalar(select(func.count(AbuseEvent.id)).where(AbuseEvent.user_id == user.id,
            AbuseEvent.event_type == 'TICKET_CREATED')) == 1
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['reserved', 'committed'])
async def test_sigkill_at_durable_boundary(stage):
    if not os.getenv('PG_TEST_URL') or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('requires disposable PostgreSQL + real Redis')
    import asyncio
    import sys
    from tests.test_qape_data import world
    from app.models.qape import AbuseEvent
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        org, _, svc, user, branch = await world(db)
        branch.is_open, branch.opens_at, branch.closes_at = True, '00:00', '23:59'
        db.add(AbusePolicy(org_id=org.id, service_id=svc.id, version=1, enabled=True, config=config()))
        await db.commit()
        user_id, agency_id, service_id = user.id, branch.id, svc.id
        key = 'sigkill_' + uuid.uuid4().hex
    env = {**os.environ, 'PYTHONPATH': os.getcwd()}
    proc = await asyncio.create_subprocess_exec(sys.executable, 'tests/qape_crash_worker.py',
        stage, os.environ['PG_TEST_URL'], agency_id, service_id, user_id, key,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    try:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=15)
        assert line.strip() == ('READY:' + stage.upper()).encode(), line
    finally:
        proc.kill()
        await proc.wait()
    async with sf() as db:
        op = (await db.scalars(select(QapeCreateOperation).where(
            QapeCreateOperation.user_id == user_id, QapeCreateOperation.client_action_id == key))).one()
        if stage == 'reserved':
            assert op.state == OperationState.RESERVED.value
            assert await db.scalar(select(func.count(Ticket.id)).where(Ticket.user_id == user_id)) == 0
            op.lease_expires_at = datetime.now(timezone.utc)-timedelta(seconds=1)
            await db.commit()
            claim = await claim_expired(db, owner='killed-worker', operation_id=op.id)
            assert len(claim) == 1
            assert await reconcile_one(db, op.id, 'killed-worker', claim[0][1]) == 'COMPENSATED'
        else:
            assert op.state == OperationState.COMMITTED.value
            assert await db.scalar(select(func.count(Ticket.id)).where(Ticket.user_id == user_id)) == 1
            assert await db.scalar(select(func.count(AbuseEvent.id)).where(
                AbuseEvent.user_id == user_id, AbuseEvent.event_type == 'TICKET_CREATED')) == 1
            replay = await TicketService(db).create_ticket(CreateTicketRequest(
                agency_id=agency_id, service_id=service_id, client_action_id=key), user_id, None)
            assert replay.id == op.ticket_id
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['DENIED', 'RECOVERY_REQUIRED'])
async def test_disabled_policy_preserves_prior_denial_or_recovery(state):
    if not os.getenv('PG_TEST_URL'):
        pytest.skip('requires disposable PostgreSQL')
    from tests.test_qape_data import world
    from app.services.qape_idempotency import payload_hash
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        org, _, svc, user, branch = await world(db)
        branch.is_open, branch.opens_at, branch.closes_at = True, '00:00', '23:59'
        policy = AbusePolicy(org_id=org.id, service_id=svc.id, version=1, enabled=False, config=config())
        db.add(policy); await db.flush()
        key = 'prior_' + uuid.uuid4().hex
        op = QapeCreateOperation(org_id=org.id, agency_id=branch.id, service_id=svc.id,
            user_id=user.id, client_action_id=key, operation_type='CREATE_TICKET',
            canonicalization_version=1, payload_hash=payload_hash(branch.id, svc.id, 'standard'),
            policy_id=policy.id, policy_version=1, state=state,
            response_status=429 if state == 'DENIED' else 503,
            response_snapshot={'code': state, 'operation_id': 'stable'})
        db.add(op); await db.commit()
        user_id, agency_id, service_id = user.id, branch.id, svc.id
        with pytest.raises(HTTPException) as result:
            await TicketService(db).create_ticket(CreateTicketRequest(
                agency_id=agency_id, service_id=service_id, client_action_id=key), user_id, None)
        assert result.value.detail == {'code': state, 'operation_id': 'stable'}
        assert await db.scalar(select(func.count(Ticket.id)).where(Ticket.user_id == user_id)) == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_sigkill_after_lua_denied_before_postgres_decision():
    if not os.getenv('PG_TEST_URL') or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('requires disposable PostgreSQL 15 and real Redis 7')
    import asyncio
    import sys
    from tests.test_qape_data import world
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        org, _, svc, user, branch = await world(db)
        branch.is_open, branch.opens_at, branch.closes_at = True, '00:00', '23:59'
        limited = config()
        limited['limits']['max_requests_per_10m'] = 1
        db.add(AbusePolicy(org_id=org.id, service_id=svc.id,
                           version=1, enabled=True, config=limited))
        await db.commit()
        org_id, user_id, agency_id, service_id = org.id, user.id, branch.id, svc.id
        seed = await reserve(org_id=org_id, service_id=service_id, user_id=user_id,
            reservation_id=str(uuid.uuid4()), attempt_generation=0,
            capacity=2, refill_seconds=300, window_seconds=600, max_requests=1)
        assert seed.allowed
        key = 'crash_denied_' + uuid.uuid4().hex
    env = {**os.environ, 'PYTHONPATH': os.getcwd()}
    proc = await asyncio.create_subprocess_exec(sys.executable, 'tests/qape_crash_worker.py',
        'denied', os.environ['PG_TEST_URL'], agency_id, service_id, user_id, key,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    try:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=15)
        assert line.strip() == b'READY:DENIED', line
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    async with sf() as db:
        op = (await db.scalars(select(QapeCreateOperation).where(
            QapeCreateOperation.client_action_id == key))).one()
        assert op.state == OperationState.PENDING.value and op.reservation_id
        redis_state, remaining, deadline = await reservation_state(org_id=org_id,
            service_id=service_id, user_id=user_id,
            reservation_id=op.reservation_id, attempt_generation=op.attempt_generation)
        assert redis_state == 'DENIED' and remaining > 0 and deadline > datetime.now(timezone.utc).timestamp()
        op.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
        claim = await claim_expired(db, owner='denial-worker', operation_id=op.id)
        assert len(claim) == 1
        assert await reconcile_one(db, op.id, 'denial-worker', claim[0][1]) == 'DENIED'
        assert op.state == OperationState.DENIED.value and op.decision_code == 'RATE_LIMITED'
        from app.models.qape import AbuseEvent
        recovered_audit = (await db.scalars(select(AbuseEvent).where(
            AbuseEvent.event_type == 'QAPE_DECISION',
            AbuseEvent.user_id == user_id, AbuseEvent.details['operation_id'].as_string() == op.id,
            AbuseEvent.details['attempt_generation'].as_integer() == 0))).all()
        assert len(recovered_audit) == 1
        assert recovered_audit[0].decision == 'TEMPORARY_BLOCK'
        assert recovered_audit[0].reason_codes == ['RATE_LIMITED']
        assert op.retry_after and op.response_status == 429
        assert await db.scalar(select(func.count(Ticket.id)).where(Ticket.user_id == user_id)) == 0
        same = CreateTicketRequest(agency_id=agency_id, service_id=service_id, client_action_id=key)
        with pytest.raises(HTTPException) as replay:
            await TicketService(db).create_ticket(same, user_id, None)
        assert replay.value.status_code == 429 and op.attempt_generation == 0
        op.retry_after = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
        with pytest.raises(HTTPException) as retried:
            await TicketService(db).create_ticket(same, user_id, None)
        assert retried.value.status_code == 429
        await db.refresh(op)
        assert op.attempt_generation == 1
    await engine.dispose()

@pytest.mark.asyncio
async def test_temporary_block_outage_expires_and_retries_same_intent():
    if not os.getenv('PG_TEST_URL') or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('requires disposable PostgreSQL 15 and real Redis 7')
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        org = Organization(name='QAPE block test', slug=uuid.uuid4().hex, sector='other')
        db.add(org); await db.flush()
        branch = Branch(name='test', slug=uuid.uuid4().hex, sector='other', org_id=org.id,
                        is_open=True, opens_at='00:00', closes_at='23:59')
        db.add(branch); await db.flush()
        svc = Service(name='test', org_id=org.id, branch_id=branch.id)
        user = User(phone='+257' + str(uuid.uuid4().int % 10**8).zfill(8))
        db.add_all([svc, user]); await db.flush()
        cfg = config()
        cfg['redis_unavailable'] = 'TEMPORARY_BLOCK'
        cfg['risk']['hard_block_seconds'] = 1
        cfg['risk']['cooldown_seconds'] = 1
        db.add(AbusePolicy(org_id=org.id, service_id=svc.id, version=1,
                           enabled=True, config=cfg))
        await db.commit()
        data = CreateTicketRequest(agency_id=branch.id, service_id=svc.id,
                                   client_action_id='block_' + uuid.uuid4().hex)
        with patch('app.core.redis.get_redis', new_callable=AsyncMock, side_effect=ConnectionError):
            with pytest.raises(HTTPException) as denied:
                await TicketService(db).create_ticket(data, user.id, None)
        assert denied.value.status_code == 503
        op = (await db.scalars(select(QapeCreateOperation).where(
            QapeCreateOperation.client_action_id == data.client_action_id))).one()
        assert op.state == OperationState.DENIED.value
        assert op.decision_code == 'REDIS_UNAVAILABLE'
        assert op.retry_after is not None
        assert await db.scalar(select(func.count(Ticket.id)).where(Ticket.user_id == user.id)) == 0
        with pytest.raises(HTTPException) as repeated:
            await TicketService(db).create_ticket(data, user.id, None)
        assert repeated.value.detail == denied.value.detail
        await asyncio.sleep(1.1)
        created = await TicketService(db).create_ticket(data, user.id, None)
        assert created.id and op.attempt_generation == 1
        audits = (await db.scalars(select(AbuseEvent).where(
            AbuseEvent.event_type == 'QAPE_DECISION',
            AbuseEvent.details['operation_id'].as_string() == op.id))).all()
        assert sorted(row.decision for row in audits) == ['ALLOW', 'TEMPORARY_BLOCK']
        assert await db.scalar(select(func.count(AbuseEvent.id)).where(
            AbuseEvent.event_type == 'TICKET_CREATED', AbuseEvent.ticket_id == created.id)) == 1
    await engine.dispose()
