"""Disposable PostgreSQL + real Redis integration tests. No production connections."""
import os
import uuid
import pytest
import pytest_asyncio
from app.core.redis import close_redis
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.organization import Organization
from app.models.branch import Branch, Service
from app.models.user import User
from app.models.ticket import Ticket
from app.models.qape import AbusePolicy, AbuseEvent
from app.models.qape_operation import QapeCreateOperation, OperationState
from app.models.queue_log import QueueLog
from app.models.event_outbox import EventOutbox
from app.schemas.ticket import CreateTicketRequest
from app.schemas.sync import SyncAction
from app.services.sync_service import SyncService
from app.services.ticket_service import TicketService
from app.services.qape_fencing import lock_for_creation_commit, StaleOperation
from app.services.qape_recovery import claim_expired, reconcile_one
from tests.test_qape_data import config
from unittest.mock import patch, AsyncMock
import asyncio
from datetime import datetime, timedelta, timezone
from app.services.qape_reservation import reserve


@pytest_asyncio.fixture(autouse=True)
async def redis_connection_lifecycle():
    yield
    await close_redis()


@pytest.mark.asyncio
async def test_qape_creation_replay_conflict_and_fencing():
    url = os.getenv('PG_TEST_URL')
    if not url or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('Requires disposable PG_TEST_URL and real QAPE_REDIS_TEST')
    engine = create_async_engine(url)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        org = Organization(name='QAPE test', slug=uuid.uuid4().hex, sector='other')
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
                                   client_action_id='qape_' + uuid.uuid4().hex)
        response = await TicketService(db).create_ticket(data, user.id, None)
        assert response.id
        replay = await TicketService(db).create_ticket(data, user.id, None)
        assert replay.model_dump(mode='json') == response.model_dump(mode='json')
        offline = await SyncService(db)._apply_one(user, SyncAction(type='create_ticket',
            client_action_id=data.client_action_id, payload={
                'agency_id': data.agency_id, 'service_id': data.service_id}))
        assert offline.status == 'already_applied' and offline.ticket['id'] == response.id
        with pytest.raises(HTTPException) as conflict:
            await TicketService(db).create_ticket(data.model_copy(update={'priority':'vip'}), user.id, None)
        assert conflict.value.status_code == 409
        assert conflict.value.detail['code'] == 'IDEMPOTENCY_PAYLOAD_CONFLICT'
        op = (await db.scalars(select(QapeCreateOperation).where(QapeCreateOperation.client_action_id == data.client_action_id))).one()
        assert op.state == OperationState.COMMITTED.value and op.ticket_id == response.id
        assert 'qr_token' not in op.response_snapshot
        assert replay.qr_token == response.qr_token
        event = (await db.scalars(select(AbuseEvent).where(AbuseEvent.ticket_id == response.id))).one()
        ticket = await db.get(Ticket, response.id)
        assert event.provenance == 'LIVE' and event.decision == 'ALLOW'
        audit = (await db.scalars(select(AbuseEvent).where(
            AbuseEvent.event_type == 'QAPE_DECISION', AbuseEvent.user_id == user.id,
            AbuseEvent.details['operation_id'].as_string() == op.id))).all()
        assert len(audit) == 1 and audit[0].decision == 'ALLOW'
        assert event.service_id == ticket.service_id and event.created_at == ticket.created_at
        assert (await db.scalar(select(func.count(AbuseEvent.id)).where(AbuseEvent.ticket_id == response.id))) == 1
        assert (await db.scalar(select(func.count(QueueLog.id)).where(QueueLog.ticket_id == response.id))) == 1
        outbox = (await db.scalars(select(EventOutbox).where(EventOutbox.org_id == org.id))).one()
        assert outbox.published_at is None  # no publish before/inside ticket commit
        second_user = User(phone='+257' + str(uuid.uuid4().int % 10**8).zfill(8))
        db.add(second_user); await db.commit()
        second_key = 'qape_' + uuid.uuid4().hex
        second = await TicketService(db).create_ticket(data.model_copy(update={'client_action_id':second_key}),
                                                       second_user.id, None)
        second_op = (await db.scalars(select(QapeCreateOperation).where(
            QapeCreateOperation.client_action_id == second_key))).one()
        own_outbox = (await db.scalars(select(EventOutbox).where(
            EventOutbox.payload['qape_operation_id'].as_string() == second_op.id))).one()
        assert own_outbox.payload['ticket_id'] == second.id
        assert outbox.payload['qape_operation_id'] == op.id
        # Removing only the second durable publication must NOT let recovery
        # accept the first ticket's nearby event as evidence for the second.
        await db.delete(own_outbox)
        second_op.state = OperationState.RESERVED.value
        second_op.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=2)
        await db.commit()
        claim = await claim_expired(db, owner='outbox-worker', operation_id=second_op.id)
        assert len(claim) == 1
        assert await reconcile_one(db, second_op.id, 'outbox-worker', claim[0][1]) == 'RECOVERY_REQUIRED'
        assert second_op.decision_code == 'INCONSISTENT_TICKET_EVENT'
        op.lease_generation += 1
        await db.commit()
        with pytest.raises(StaleOperation):
            await lock_for_creation_commit(db, operation_id=op.id, owner=op.lease_owner,
                                           lease_generation=op.lease_generation - 1)
        await db.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_two_workers_fence_once_and_compensate():
    if not os.getenv('PG_TEST_URL') or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('requires disposable PostgreSQL + real Redis')
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        org = Organization(name='recovery test', slug=uuid.uuid4().hex, sector='other')
        db.add(org); await db.flush()
        branch = Branch(name='test', slug=uuid.uuid4().hex, sector='other', org_id=org.id)
        db.add(branch); await db.flush()
        svc = Service(name='test', org_id=org.id, branch_id=branch.id)
        user = User(phone='+257' + str(uuid.uuid4().int % 10**8).zfill(8))
        db.add_all([svc, user]); await db.flush()
        rid = str(uuid.uuid4())
        assert (await reserve(org_id=org.id, service_id=svc.id, user_id=user.id,
            reservation_id=rid, attempt_generation=0, capacity=2, refill_seconds=3600,
            window_seconds=600, max_requests=2)).allowed
        op = QapeCreateOperation(user_id=user.id, org_id=org.id, agency_id=branch.id,
            service_id=svc.id, client_action_id='recover_' + uuid.uuid4().hex,
            payload_hash='a'*64, canonicalization_version=1, operation_type='CREATE_TICKET',
            state=OperationState.RESERVED.value, reservation_id=rid, reservation_capacity=2,
            lease_owner='original', lease_expires_at=datetime.now(timezone.utc)-timedelta(seconds=10),
            attempt_generation=0, lease_generation=0)
        db.add(op); await db.commit()
        op_id = op.id
    async def worker(owner):
        async with sf() as db:
            return await claim_expired(db, owner=owner, limit=1, operation_id=op_id)
    claims = await asyncio.gather(worker('worker1'), worker('worker2'))
    winners = [(owner, rows[0]) for owner, rows in zip(('worker1','worker2'), claims) if rows]
    assert len(winners) == 1 and winners[0][1][0] == op_id
    owner, (_, epoch) = winners[0]
    async with sf() as db:
        with pytest.raises(StaleOperation):
            await lock_for_creation_commit(db, operation_id=op_id, owner='original', lease_generation=0)
        await db.rollback()
        assert await reconcile_one(db, op_id, owner, epoch) == OperationState.COMPENSATED.value
        assert await reconcile_one(db, op_id, owner, epoch) == OperationState.COMPENSATED.value
        row = await db.get(QapeCreateOperation, op_id)
        assert row.ticket_id is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_parallel_same_key_no_double_ticket_or_event():
    if not os.getenv('PG_TEST_URL') or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('requires disposable PostgreSQL + real Redis')
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        org = Organization(name='parallel test', slug=uuid.uuid4().hex, sector='other')
        db.add(org); await db.flush()
        branch = Branch(name='test', slug=uuid.uuid4().hex, sector='other', org_id=org.id,
                        is_open=True, opens_at='00:00', closes_at='23:59')
        db.add(branch); await db.flush()
        svc = Service(name='test', org_id=org.id, branch_id=branch.id)
        user = User(phone='+257' + str(uuid.uuid4().int % 10**8).zfill(8))
        db.add_all([svc, user]); await db.flush()
        db.add(AbusePolicy(org_id=org.id, service_id=svc.id, version=1, enabled=True, config=config()))
        await db.commit()
        user_id = user.id
        key = 'parallel_' + uuid.uuid4().hex
        data = CreateTicketRequest(agency_id=branch.id, service_id=svc.id, client_action_id=key)
    async def send():
        async with sf() as db:
            try:
                return await TicketService(db).create_ticket(data, user_id, None)
            except HTTPException as exc:
                return exc
    outcomes = await asyncio.gather(send(), send())
    assert any(not isinstance(x, HTTPException) for x in outcomes)
    assert all(not isinstance(x, HTTPException) or x.status_code == 409 for x in outcomes)
    async with sf() as db:
        assert await db.scalar(select(func.count(Ticket.id)).where(Ticket.user_id == user_id)) == 1
        assert await db.scalar(select(func.count(AbuseEvent.id)).where(
            AbuseEvent.user_id == user_id, AbuseEvent.event_type == 'TICKET_CREATED')) == 1
        assert await db.scalar(select(func.count(QapeCreateOperation.id)).where(
            QapeCreateOperation.user_id == user_id, QapeCreateOperation.client_action_id == key)) == 1
    await engine.dispose()

@pytest.mark.asyncio
async def test_parallel_distinct_intents_one_user_daily_quota_one_creation():
    if not os.getenv('PG_TEST_URL') or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('requires disposable PostgreSQL 15 and real Redis 7')
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        org = Organization(name='parallel quota', slug=uuid.uuid4().hex, sector='other')
        db.add(org); await db.flush()
        branch = Branch(name='test', slug=uuid.uuid4().hex, sector='other', org_id=org.id,
                        is_open=True, opens_at='00:00', closes_at='23:59')
        db.add(branch); await db.flush()
        svc = Service(name='test', org_id=org.id, branch_id=branch.id)
        user = User(phone='+257' + str(uuid.uuid4().int % 10**8).zfill(8))
        db.add_all([svc, user]); await db.flush()
        cfg = config()
        cfg['limits']['max_tickets_per_day'] = 1
        cfg['limits']['bucket_capacity'] = 3
        cfg['limits']['max_requests_per_10m'] = 10
        db.add(AbusePolicy(org_id=org.id, service_id=svc.id, version=1,
                           enabled=True, config=cfg))
        await db.commit()
        org_id, agency_id, service_id, user_id = org.id, branch.id, svc.id, user.id
    async def send(key):
        async with sf() as db:
            try:
                return await TicketService(db).create_ticket(CreateTicketRequest(
                    agency_id=agency_id, service_id=service_id,
                    client_action_id=key), user_id, None)
            except HTTPException as exc:
                return exc
    outcomes = await asyncio.gather(send('quota_a_' + uuid.uuid4().hex),
                                    send('quota_b_' + uuid.uuid4().hex))
    assert sum(not isinstance(row, HTTPException) for row in outcomes) == 1
    async with sf() as db:
        assert await db.scalar(select(func.count(Ticket.id)).where(Ticket.user_id == user_id)) == 1
        assert await db.scalar(select(func.count(AbuseEvent.id)).where(
            AbuseEvent.org_id == org_id, AbuseEvent.user_id == user_id,
            AbuseEvent.event_type == 'TICKET_CREATED')) == 1
    await engine.dispose()
