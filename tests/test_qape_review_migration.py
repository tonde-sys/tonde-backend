"""Disposable PostgreSQL 15 migration/legacy-audit checks; never production."""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.qape_operation import QapeCreateOperation
from tests.test_qape_review import setup_review

ROOT = Path(__file__).resolve().parents[1]


def _admin_url():
    from sqlalchemy.engine.url import make_url
    url = make_url(os.environ['PG_TEST_URL'])
    return url.set(database='postgres')


def _database_url(db_name):
    # URL.__str__ masks credentials; Alembic needs the actual password.
    return _admin_url().set(database=db_name).render_as_string(hide_password=False)


def _migration(db_url, revision, *, authorize=False):
    env = {**os.environ, 'DATABASE_URL': db_url, 'MIGRATION_DATABASE_URL': ''}
    env.pop('QAPE_REVIEW_BACKFILL_AUTHORIZED', None)
    if authorize:
        env['QAPE_REVIEW_BACKFILL_AUTHORIZED'] = 'explicitly-approved-after-dry-run'
    return subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', revision],
                          cwd=ROOT, env=env, capture_output=True, text=True)


async def _seed_legacy(db_url, *, state, actor_is_requester, action):
    engine = create_async_engine(db_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            org, _, branch, svc, client, agent, *_ = await setup_review(db)
            operation = QapeCreateOperation(
                user_id=client, operation_type='CREATE_TICKET',
                client_action_id=uuid.uuid4().hex, payload_hash='a' * 64,
                canonicalization_version=1, org_id=org, agency_id=branch,
                service_id=svc, state=state, policy_version=1,
                review_generation=0,
                decision_code='HUMAN_REJECTED' if action == 'REJECT' else 'REDIS_UNAVAILABLE')
            db.add(operation)
            await db.flush()
            actor = client if actor_is_requester else agent
            legacy_id = hashlib.sha256(
                f'qape-human-review:v1:{operation.id}'.encode()).hexdigest()
            await db.execute(text('''INSERT INTO abuse_events
                (id, org_id, user_id, service_id, request_id, event_type,
                 provenance, decision, reason_codes, policy_version, created_at, metadata)
                VALUES (:id, :org, :client, :svc, :request_id, 'QAPE_HUMAN_REVIEW',
                        'LIVE', :decision, CAST(:reason_codes AS jsonb), 1,
                        now(), CAST(:details AS jsonb))'''), {
                'id': str(uuid.uuid4()), 'org': org, 'client': client,
                'svc': svc, 'request_id': legacy_id, 'decision': action,
                'reason_codes': json.dumps([
                    'HUMAN_APPROVED' if action == 'APPROVE' else 'HUMAN_REJECTED']),
                'details': json.dumps({'operation_id': operation.id,
                                       'actor_id': actor, 'reason': 'Ancien contrôle'})})
            await db.commit()
            return operation.id, legacy_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('state,actor_is_requester,action,blocked,expected', [
    ('REVIEW_REQUIRED', True, 'APPROVE', True, None),
    ('DENIED', True, 'REJECT', False, 1),
    ('REVIEW_REQUIRED', False, 'APPROVE', False, 2),
])
async def test_legacy_migration_preserves_audit_or_blocks(
        state, actor_is_requester, action, blocked, expected):
    if not os.getenv('PG_TEST_URL') or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('requires disposable PostgreSQL 15 and Redis 7')
    from sqlalchemy import create_engine
    name = 'qape018_migration_' + uuid.uuid4().hex
    admin = create_engine(_admin_url().set(drivername='postgresql+psycopg2'),
                          isolation_level='AUTOCOMMIT')
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        url = _database_url(name)
        first = _migration(url, '017_qape_create_operations')
        assert first.returncode == 0, "Alembic 017 failed on disposable database"
        op_id, audit_id = await _seed_legacy(url, state=state,
            actor_is_requester=actor_is_requester, action=action)
        second = _migration(url, 'head', authorize=True)
        assert (second.returncode != 0) == blocked, "Unexpected Alembic 018 result"
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                version = await conn.scalar(text('SELECT version_num FROM alembic_version'))
                assert version == ('017_qape_create_operations' if blocked
                                   else '018_qape_review_generation')
                actual = await conn.scalar(text('''SELECT review_generation
                    FROM qape_create_operations WHERE id=:id'''), {'id': op_id})
                assert actual == (0 if blocked else expected)
                assert await conn.scalar(text('''SELECT count(*) FROM abuse_events
                    WHERE request_id=:audit AND event_type='QAPE_HUMAN_REVIEW' '''),
                    {'audit': audit_id}) == 1
                if not blocked:
                    fields = (await conn.execute(text('''SELECT review_audit_version,
                        review_operation_id, review_generation FROM abuse_events
                        WHERE request_id=:audit'''), {'audit': audit_id})).one()
                    assert fields == (None, None, None)
        finally:
            await engine.dispose()
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()

@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['HUMAN_APPROVED', 'HUMAN_REJECTED'])
async def test_human_code_without_any_audit_blocks_migration(code):
    """The operation EXISTS must not be nested under the empty audit table."""
    if not os.getenv('PG_TEST_URL') or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('requires disposable PostgreSQL 15 and Redis 7')
    from sqlalchemy import create_engine
    name = 'qape018_no_audit_' + uuid.uuid4().hex
    admin = create_engine(_admin_url().set(drivername='postgresql+psycopg2'),
                          isolation_level='AUTOCOMMIT')
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        url = _database_url(name)
        first = _migration(url, '017_qape_create_operations')
        assert first.returncode == 0, "Alembic 017 failed on disposable database"
        engine = create_async_engine(url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                org, _, branch, svc, client, *_ = await setup_review(db)
                operation = QapeCreateOperation(
                    user_id=client, operation_type='CREATE_TICKET',
                    client_action_id=uuid.uuid4().hex, payload_hash='a' * 64,
                    canonicalization_version=1, org_id=org, agency_id=branch,
                    service_id=svc, state='DENIED', policy_version=1,
                    review_generation=0, decision_code=code)
                db.add(operation)
                await db.commit()
                op_id = operation.id
        finally:
            await engine.dispose()
        second = _migration(url, 'head', authorize=True)
        assert second.returncode != 0, 'missing human audit must block migration'
        assert bool('Ambiguous QAPE human audit' in second.stderr), 'Expected audit guard did not fire'
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                assert await conn.scalar(text('SELECT count(*) FROM abuse_events')) == 0
                assert await conn.scalar(text('SELECT version_num FROM alembic_version')) == '017_qape_create_operations'
                assert await conn.scalar(text('''SELECT decision_code
                    FROM qape_create_operations WHERE id=:id'''),
                    {'id': op_id}) == code
        finally:
            await engine.dispose()
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('version,operation_id,generation', [
    (None, 'partial-operation', None),
    (None, None, 1),
    (2, None, 1),
    (2, 'partial-operation', None),
])
async def test_partially_filled_audit_fails_sql_check(version, operation_id, generation):
    if not os.getenv('PG_TEST_URL') or not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('requires disposable PostgreSQL 15 and Redis 7')
    from sqlalchemy.exc import IntegrityError
    engine = create_async_engine(os.environ['PG_TEST_URL'])
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            org, _, branch, svc, client, *_ = await setup_review(db)
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    await db.execute(text('''INSERT INTO abuse_events
                        (id, org_id, service_id, user_id, request_id,
                         event_type, provenance, decision, reason_codes,
                         policy_version, created_at, metadata,
                         review_audit_version, review_operation_id,
                         review_generation)
                        VALUES (:id, :org, :svc, :client, :request_id,
                         'QAPE_HUMAN_REVIEW', 'LIVE', 'APPROVE',
                         CAST(:codes AS jsonb), 1, now(),
                         CAST(:metadata AS jsonb), :version, :operation_id,
                         :generation)'''), {
                        'id': str(uuid.uuid4()), 'org': org, 'svc': svc,
                        'client': client, 'request_id': uuid.uuid4().hex,
                        'codes': json.dumps(['HUMAN_APPROVED']),
                        'metadata': json.dumps({'operation_id': str(uuid.uuid4()),
                                               'actor_id': str(uuid.uuid4()),
                                               'reason': 'synthetic'}),
                        'version': version, 'operation_id': operation_id,
                        'generation': generation})
            await db.rollback()
    finally:
        await engine.dispose()


def test_review_generation_only_on_real_state_transition():
    """A technical replay cannot increment without a new state transition."""
    from app.models.qape_operation import OperationState
    operation = QapeCreateOperation(state=OperationState.PENDING.value,
                                    review_generation=0)
    operation.transition(OperationState.REVIEW_REQUIRED)
    assert operation.review_generation == 1
    with pytest.raises(ValueError):
        operation.transition(OperationState.REVIEW_REQUIRED)
    assert operation.review_generation == 1
    operation.transition(OperationState.PENDING, human_authorized=True)
    assert operation.review_generation == 1
    operation.transition(OperationState.REVIEW_REQUIRED)
    assert operation.review_generation == 2
    with pytest.raises(ValueError):
        operation.transition(OperationState.REVIEW_REQUIRED)
    assert operation.review_generation == 2
