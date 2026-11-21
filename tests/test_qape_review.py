"""Phase 1E integration: human action never inserts a ticket itself."""
import asyncio
import os
import uuid
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.core.redis import close_redis
from app.models.organization import Organization
from app.models.branch import Branch, Service
from app.models.user import User, UserRole
from app.models.employee import Employee, EmployeeStatus
from app.models.ticket import Ticket, TicketStatus
from app.models.qape import AbusePolicy, AbuseEvent, AbuseRestriction
from app.models.qape_operation import QapeCreateOperation, OperationState
from app.schemas.ticket import CreateTicketRequest
from app.services.qape_review import QapeReviewService
from app.services.ticket_service import TicketService
from tests.test_qape_data import config


@pytest_asyncio.fixture(autouse=True)
async def redis_lifecycle():
    yield
    await close_redis()


async def setup_review(db, *, mode='REVIEW_REQUIRED'):
    org = Organization(name='review', slug=uuid.uuid4().hex, sector='other')
    other = Organization(name='other review', slug=uuid.uuid4().hex, sector='other')
    db.add_all([org, other]); await db.flush()
    branch = Branch(name='test', slug=uuid.uuid4().hex, sector='other', org_id=org.id,
                    is_open=True, opens_at='00:00', closes_at='23:59')
    db.add(branch); await db.flush()
    svc = Service(name='test', org_id=org.id, branch_id=branch.id)
    client = User(phone='+257' + str(uuid.uuid4().int % 10**8).zfill(8))
    agent = User(phone='+257' + str(uuid.uuid4().int % 10**8).zfill(8),
                 role=UserRole.SUPERVISOR, org_id=org.id)
    other_agent = User(phone='+257' + str(uuid.uuid4().int % 10**8).zfill(8),
                       role=UserRole.SUPERVISOR, org_id=other.id)
    ordinary = User(phone='+257' + str(uuid.uuid4().int % 10**8).zfill(8),
                    role=UserRole.AGENT, org_id=org.id)
    db.add_all([svc, client, agent, other_agent, ordinary]); await db.flush()
    db.add(Employee(org_id=org.id, agency_id=branch.id, user_id=agent.id,
                    role=UserRole.SUPERVISOR, status=EmployeeStatus.ACTIVE))
    policy_config = config()
    policy_config['redis_unavailable'] = mode
    db.add(AbusePolicy(org_id=org.id, service_id=svc.id, version=1,
                       enabled=True, config=policy_config))
    await db.commit()
    ids = org.id, other.id, branch.id, svc.id, client.id, agent.id, other_agent.id, ordinary.id
    return ids


async def make_review(db, ids):
    org, _, branch, svc, client, *_ = ids
    data = CreateTicketRequest(agency_id=branch, service_id=svc,
                               client_action_id='review_' + uuid.uuid4().hex)
    with patch('app.core.redis.get_redis', new_callable=AsyncMock, side_effect=ConnectionError):
        with pytest.raises(HTTPException) as result:
            await TicketService(db).create_ticket(data, client, None)
    assert result.value.status_code == 202
    row = (await db.scalars(select(QapeCreateOperation).where(
        QapeCreateOperation.client_action_id == data.client_action_id))).one()
    return data, row.id


@pytest.mark.asyncio
async def test_review_scope_reject_and_idempotent_audit():
    if not os.getenv('PG_TEST_URL') or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('requires disposable PostgreSQL 15 and Redis 7')
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        ids = await setup_review(db)
        org, other, branch, svc, client, agent, outside, ordinary = ids
        data, op_id = await make_review(db, ids)
        staff = await db.get(User, agent)
        elsewhere = await db.get(User, outside)
        low = await db.get(User, ordinary)
        review = QapeReviewService(db)
        page = await review.pending(staff, org, branch, svc, 1, 10)
        assert page['total'] == 1 and page['items'][0]['operation_id'] == op_id
        item = page['items'][0]
        assert item['user_id'] == client and item['service_id'] == svc
        assert item['reason_codes'] == ['REDIS_UNAVAILABLE']
        assert item['policy_version'] == 1 and item['created_at']
        assert not {'payload_hash', 'response_snapshot', 'reservation_id', 'qr_token'} & item.keys()
        assert (await review.pending(staff, org, branch, svc, 2, 10))['items'] == []
        other_branch = Branch(name='other branch', slug=uuid.uuid4().hex,
                              sector='other', org_id=org)
        db.add(other_branch); await db.flush()
        other_supervisor = User(phone='+257' + str(uuid.uuid4().int % 10**8).zfill(8),
                                role=UserRole.SUPERVISOR, org_id=org)
        db.add(other_supervisor); await db.flush()
        db.add(Employee(org_id=org, agency_id=other_branch.id,
                        user_id=other_supervisor.id, role=UserRole.SUPERVISOR,
                        status=EmployeeStatus.ACTIVE))
        await db.commit()
        for actor in (elsewhere, low, other_supervisor):
            with pytest.raises(HTTPException) as forbidden:
                await review.pending(actor, org, branch, None, 1, 10)
            assert forbidden.value.status_code == 403
            with pytest.raises(HTTPException) as forbidden:
                await review.decide(actor, org, op_id, 'REJECT', 'Justification', 1)
            assert forbidden.value.status_code == 403
        with pytest.raises(HTTPException) as missing:
            await review.decide(staff, other, op_id, 'REJECT', 'Justification', 1)
        assert missing.value.status_code == 404
        with pytest.raises(HTTPException) as invalid:
            await review.decide(staff, org, op_id, 'REJECT', '  ', 1)
        assert invalid.value.status_code == 422
        result = await review.decide(staff, org, op_id, 'REJECT', 'Contrôle manuel', 1)
        assert result['state'] == OperationState.DENIED.value
        assert (await review.decide(staff, org, op_id, 'REJECT', 'Contrôle manuel', 1))['decided_at'] == result['decided_at']
        with pytest.raises(HTTPException) as conflicting:
            await review.decide(staff, org, op_id, 'APPROVE', 'Contrôle manuel', 1)
        assert conflicting.value.status_code == 409
        audit = (await db.scalars(select(AbuseEvent).where(
            AbuseEvent.event_type == 'QAPE_HUMAN_REVIEW',
            AbuseEvent.details['operation_id'].as_string() == op_id))).all()
        assert len(audit) == 1
        assert audit[0].details['actor_id'] == agent
        assert audit[0].details['reason'] == 'Contrôle manuel'
        assert audit[0].decision == 'REJECT' and audit[0].created_at
        assert await db.scalar(select(func.count(Ticket.id)).where(Ticket.user_id == client)) == 0
        with pytest.raises(HTTPException) as retry:
            await TicketService(db).create_ticket(data, client, None)
        assert retry.value.detail['code'] == 'HUMAN_REJECTED'
    await engine.dispose()


@pytest.mark.asyncio
async def test_review_approve_client_replays_with_controls_and_one_ticket():
    if not os.getenv('PG_TEST_URL') or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('requires disposable PostgreSQL 15 and Redis 7')
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        ids = await setup_review(db)
        org, _, branch, svc, client, agent, *_ = ids
        data, op_id = await make_review(db, ids)
        review = QapeReviewService(db)
        staff = await db.get(User, agent)
        decision = await review.decide(staff, org, op_id, 'APPROVE', 'Identité vérifiée', 1)
        assert decision['state'] == OperationState.PENDING.value
        assert await db.scalar(select(func.count(Ticket.id)).where(Ticket.user_id == client)) == 0
        with pytest.raises(HTTPException) as mismatch:
            await TicketService(db).create_ticket(data.model_copy(update={'priority': 'vip'}), client, None)
        assert mismatch.value.detail['code'] == 'IDEMPOTENCY_PAYLOAD_CONFLICT'
        # A continued outage opens a NEW review; the old request is stale.
        with patch('app.core.redis.get_redis', new_callable=AsyncMock, side_effect=ConnectionError):
            with pytest.raises(HTTPException) as still_down:
                await TicketService(db).create_ticket(data, client, None)
        assert still_down.value.status_code == 202
        with pytest.raises(HTTPException) as stale:
            await review.decide(staff, org, op_id, 'APPROVE', 'Identité vérifiée', 1)
        assert stale.value.detail['code'] == 'STALE_REVIEW_GENERATION'
        assert (await db.get(QapeCreateOperation, op_id)).review_generation == 2
        assert await db.scalar(select(func.count(AbuseEvent.id)).where(
            AbuseEvent.event_type == 'QAPE_DECISION',
            AbuseEvent.decision == 'REVIEW_REQUIRED',
            AbuseEvent.details['operation_id'].as_string() == op_id)) == 2
        await review.decide(staff, org, op_id, 'APPROVE', 'Identité vérifiée', 2)
        created = await TicketService(db).create_ticket(data, client, None)
        assert created.id
        assert (await TicketService(db).create_ticket(data, client, None)).id == created.id
        assert await db.scalar(select(func.count(AbuseEvent.id)).where(
            AbuseEvent.event_type == 'TICKET_CREATED', AbuseEvent.ticket_id == created.id)) == 1
        assert await db.scalar(select(func.count(AbuseEvent.id)).where(
            AbuseEvent.event_type == 'QAPE_HUMAN_REVIEW',
            AbuseEvent.details['operation_id'].as_string() == op_id)) == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_two_agents_only_one_human_decision():
    if not os.getenv('PG_TEST_URL') or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('requires disposable PostgreSQL 15 and Redis 7')
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        ids = await setup_review(db)
        org, _, branch, svc, client, first, *_ = ids
        second = User(phone='+257' + str(uuid.uuid4().int % 10**8).zfill(8),
                      role=UserRole.SUPERVISOR, org_id=org)
        db.add(second); await db.flush()
        db.add(Employee(org_id=org, agency_id=branch, user_id=second.id,
                        role=UserRole.SUPERVISOR, status=EmployeeStatus.ACTIVE))
        await db.commit()
        second_id = second.id
        _, op_id = await make_review(db, ids)
    async def act(actor_id, action):
        async with sf() as session:
            try:
                return await QapeReviewService(session).decide(
                    await session.get(User, actor_id), org, op_id, action, 'Vérifié', 1)
            except HTTPException as error:
                return error
    results = await asyncio.gather(act(first, 'APPROVE'), act(second_id, 'REJECT'))
    assert sum(not isinstance(row, HTTPException) for row in results) == 1
    assert sum(isinstance(row, HTTPException) and row.status_code == 409 for row in results) == 1
    async with sf() as db:
        assert await db.scalar(select(func.count(AbuseEvent.id)).where(
            AbuseEvent.event_type == 'QAPE_HUMAN_REVIEW',
            AbuseEvent.details['operation_id'].as_string() == op_id)) == 1
    await engine.dispose()

@pytest.mark.asyncio
async def test_approval_rechecks_restriction_and_does_not_create():
    if not os.getenv('PG_TEST_URL') or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('requires disposable PostgreSQL 15 and Redis 7')
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        ids = await setup_review(db)
        org, _, branch, svc, client, agent, *_ = ids
        data, op_id = await make_review(db, ids)
        await QapeReviewService(db).decide(await db.get(User, agent), org,
                                           op_id, 'APPROVE', 'Vérification faite', 1)
        now = datetime.now(timezone.utc)
        db.add(AbuseRestriction(org_id=org, service_id=svc, user_id=client,
            restriction_type='TEMPORARY_BLOCK', reason_code='EXPLICIT_RESTRICTION',
            starts_at=now, expires_at=now.replace(year=now.year + 1)))
        await db.commit()
        with pytest.raises(HTTPException) as refused:
            await TicketService(db).create_ticket(data, client, None)
        assert refused.value.status_code == 429
        assert refused.value.detail['code'] == 'ACTIVE_RESTRICTION'
        assert await db.scalar(select(func.count(Ticket.id)).where(Ticket.user_id == client)) == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_approval_then_business_failure_preserves_recovery():
    if not os.getenv('PG_TEST_URL') or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('requires disposable PostgreSQL 15 and Redis 7')
    from app.services.qape_recovery import claim_expired, reconcile_one
    from datetime import timedelta
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        ids = await setup_review(db)
        org, _, branch, svc, client, agent, *_ = ids
        data, op_id = await make_review(db, ids)
        await QapeReviewService(db).decide(await db.get(User, agent), org,
                                           op_id, 'APPROVE', 'Vérification faite', 1)
        service = await db.get(Service, svc)
        service.is_active = False
        await db.commit()
        with pytest.raises(HTTPException) as refused:
            await TicketService(db).create_ticket(data, client, None)
        assert refused.value.status_code == 400
        op = await db.get(QapeCreateOperation, op_id)
        assert op.state == OperationState.RECOVERY_REQUIRED.value
        assert await db.scalar(select(func.count(Ticket.id)).where(Ticket.user_id == client)) == 0
        op.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
        claims = await claim_expired(db, owner='review-worker', operation_id=op_id)
        assert len(claims) == 1
        assert await reconcile_one(db, op_id, 'review-worker', claims[0][1]) == 'COMPENSATED'
        assert await db.scalar(select(func.count(AbuseEvent.id)).where(
            AbuseEvent.event_type == 'TICKET_CREATED', AbuseEvent.user_id == client)) == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_review_commit_ack_lost_is_idempotent_across_sessions():
    if not os.getenv('PG_TEST_URL'):
        pytest.skip('requires disposable PostgreSQL 15')
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        ids = await setup_review(db)
        org, _, branch, svc, client, agent, *_ = ids
        _, op_id = await make_review(db, ids)
        original = db.commit
        async def commit_then_lose_ack():
            await original()
            raise ConnectionError('simulated lost acknowledgment after commit')
        with patch.object(db, 'commit', commit_then_lose_ack):
            with pytest.raises(ConnectionError):
                await QapeReviewService(db).decide(await db.get(User, agent), org,
                                                   op_id, 'REJECT', 'Motif de sécurité', 1)
    async with sf() as db:
        actor = await db.get(User, agent)
        replay = await QapeReviewService(db).decide(actor, org, op_id,
                                                    'REJECT', 'Motif de sécurité', 1)
        assert replay['state'] == OperationState.DENIED.value
        assert await db.scalar(select(func.count(AbuseEvent.id)).where(
            AbuseEvent.event_type == 'QAPE_HUMAN_REVIEW',
            AbuseEvent.details['operation_id'].as_string() == op_id)) == 1
    await engine.dispose()

@pytest.mark.asyncio
async def test_review_http_auth_and_branch_scope():
    if not os.getenv('PG_TEST_URL') or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('requires disposable PostgreSQL 15 and Redis 7')
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.core.deps import get_current_supervisor
    from app.core.database import get_db
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        ids = await setup_review(db)
        org, other, branch, svc, client, agent, outside, low = ids
        _, op_id = await make_review(db, ids)
        async def provide_db():
            yield db
        app.dependency_overrides[get_db] = provide_db
        url = f'/api/v1/qape/organizations/{org}/branches/{branch}/reviews'
        decision_url = f'/api/v1/qape/organizations/{org}/reviews/{op_id}/decision'
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as http:
                assert (await http.get(url)).status_code == 401
                async def provide_actor():
                    return await db.get(User, agent)
                app.dependency_overrides[get_current_supervisor] = provide_actor
                page = await http.get(url, params={'page': 1, 'page_size': 1})
                assert page.status_code == 200 and page.json()['total'] == 1
                assert page.json()['items'][0]['review_generation'] == 1
                blank = await http.post(decision_url, json={'action': 'APPROVE', 'reason': '  ',
                                                            'review_generation': 1})
                assert blank.status_code == 422
                async def provide_outside():
                    return await db.get(User, outside)
                app.dependency_overrides[get_current_supervisor] = provide_outside
                assert (await http.get(url)).status_code == 403
                assert (await http.post(decision_url, json={
                    'action': 'REJECT', 'reason': 'Contrôle',
                    'review_generation': 1})).status_code == 403
                async def provide_low():
                    return await db.get(User, low)
                app.dependency_overrides[get_current_supervisor] = provide_low
                assert (await http.get(url)).status_code == 403
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(get_current_supervisor, None)
    await engine.dispose()

@pytest.mark.asyncio
async def test_review_sigkill_after_decision_commit_recovers_replay():
    if not os.getenv('PG_TEST_URL') or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('requires disposable PostgreSQL 15 and Redis 7')
    import sys
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        ids = await setup_review(db)
        org, _, branch, svc, client, agent, *_ = ids
        data, op_id = await make_review(db, ids)
    proc = await asyncio.create_subprocess_exec(sys.executable,
        'tests/qape_review_crash_worker.py', os.environ['PG_TEST_URL'],
        org, op_id, agent, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env={**os.environ, 'PYTHONPATH': os.getcwd()})
    try:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=15)
        assert line.strip() == b'READY:REVIEW_COMMITTED', (line, await proc.stderr.read())
    finally:
        proc.kill()
        await proc.wait()
    async with sf() as db:
        assert (await db.get(QapeCreateOperation, op_id)).state == OperationState.DENIED.value
        actor = await db.get(User, agent)
        repeated = await QapeReviewService(db).decide(actor, org, op_id,
                                                       'REJECT', 'Refus motivé', 1)
        assert repeated['state'] == OperationState.DENIED.value
        assert await db.scalar(select(func.count(AbuseEvent.id)).where(
            AbuseEvent.event_type == 'QAPE_HUMAN_REVIEW',
            AbuseEvent.details['operation_id'].as_string() == op_id)) == 1
        with pytest.raises(HTTPException) as blocked:
            await TicketService(db).create_ticket(data, client, None)
        assert blocked.value.detail['code'] == 'HUMAN_REJECTED'
    await engine.dispose()

@pytest.mark.asyncio
async def test_second_review_concurrent_same_approval_and_client_replay_once():
    if not os.getenv('PG_TEST_URL') or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('requires disposable PostgreSQL 15 and Redis 7')
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        ids = await setup_review(db)
        org, _, branch, svc, client, agent, *_ = ids
        data, op_id = await make_review(db, ids)
        staff = await db.get(User, agent)
        await QapeReviewService(db).decide(staff, org, op_id, 'APPROVE', 'Vérifié', 1)
        with patch('app.core.redis.get_redis', new_callable=AsyncMock, side_effect=ConnectionError):
            with pytest.raises(HTTPException) as still_down:
                await TicketService(db).create_ticket(data, client, None)
        assert still_down.value.status_code == 202
    async def approve():
        async with sf() as session:
            return await QapeReviewService(session).decide(
                await session.get(User, agent), org, op_id, 'APPROVE', 'Vérifié', 2)
    approvals = await asyncio.gather(approve(), approve())
    assert all(result['state'] == OperationState.PENDING.value for result in approvals)
    async def replay():
        async with sf() as session:
            try:
                return await TicketService(session).create_ticket(data, client, None)
            except HTTPException as exc:
                return exc
    replies = await asyncio.gather(replay(), replay())
    successes = [row for row in replies if not isinstance(row, HTTPException)]
    assert successes and len({row.id for row in successes}) == 1
    assert all(not isinstance(row, HTTPException) or row.status_code == 409
               for row in replies)
    async with sf() as db:
        assert await db.scalar(select(func.count(Ticket.id)).where(
            Ticket.user_id == client, Ticket.client_action_id == data.client_action_id)) == 1
        assert await db.scalar(select(func.count(AbuseEvent.id)).where(
            AbuseEvent.event_type == 'QAPE_HUMAN_REVIEW',
            AbuseEvent.details['operation_id'].as_string() == op_id)) == 2
        from app.core.redis import get_redis
        from app.services.qape_reservation import _keys
        operation = await db.get(QapeCreateOperation, op_id)
        keys, _ = _keys(org, svc, client, operation.reservation_id,
                        operation.attempt_generation)
        assert await (await get_redis()).zcard(keys[1]) == 1
    await engine.dispose()

@pytest.mark.asyncio
async def test_self_review_denied_for_both_actions_and_other_supervisor_allowed():
    if not os.getenv('PG_TEST_URL'):
        pytest.skip('requires disposable PostgreSQL 15')
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        ids = await setup_review(db)
        org, _, branch, svc, client, agent, *_ = ids
        _, op_id = await make_review(db, ids)
        requester = await db.get(User, client)
        requester.role = UserRole.SUPERVISOR
        requester.org_id = org
        db.add(Employee(org_id=org, agency_id=branch, user_id=client,
                        role=UserRole.SUPERVISOR, status=EmployeeStatus.ACTIVE))
        await db.commit()
        review = QapeReviewService(db)
        for action in ('APPROVE', 'REJECT'):
            with pytest.raises(HTTPException) as forbidden:
                await review.decide(requester, org, op_id, action, 'Contrôle', 1)
            assert forbidden.value.status_code == 403
            assert forbidden.value.detail['code'] == 'QAPE_SELF_REVIEW_FORBIDDEN'
        assert (await review.decide(await db.get(User, agent), org, op_id,
                                    'APPROVE', 'Contrôle', 1))['state'] == 'PENDING'
    await engine.dispose()
