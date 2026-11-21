"""PostgreSQL-only tenant constraints and partial indexes; disposable PG_TEST_URL."""
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.qape import AbusePolicy, AbuseEvent
from app.schemas.qape import PolicyInput, EventInput
from app.services.qape_data import PolicyRepository, EventRepository
from tests.test_qape_data import world, config


@pytest.mark.asyncio
async def test_postgres_constraints_and_jsonb():
    url = os.getenv("PG_TEST_URL")
    if not url:
        pytest.skip("Requires disposable PostgreSQL with alembic upgrade head")
    engine = create_async_engine(url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            org, other, svc, user, _ = await world(db)
            repo = PolicyRepository(db)
            org_id, other_id, service_id = org.id, other.id, svc.id
            row = await repo.create(PolicyInput(org_id=org_id, service_id=service_id,
                version=1, enabled=True, config=config()))
            row_id = row.id
            await db.commit()
            assert (await db.execute(text("SELECT pg_typeof(config)::text FROM abuse_policies WHERE id=:id"),
                                     {"id": row_id})).scalar_one() == "jsonb"
            # Direct SQL cannot bypass the composite FK even if a repository is bypassed.
            with pytest.raises(IntegrityError):
                await db.execute(text("UPDATE abuse_policies SET org_id=:wrong WHERE id=:id"),
                                 {"wrong": other_id, "id": row_id})
            await db.rollback()
            with pytest.raises(IntegrityError):
                await repo.create(PolicyInput(org_id=org_id, service_id=service_id,
                    version=2, enabled=True, config=config()))
            await db.rollback()
            # Direct insert violating version uniqueness even if policy disabled.
            with pytest.raises(IntegrityError):
                await repo.create(PolicyInput(org_id=org_id, service_id=service_id,
                    version=1, enabled=False, config=config()))
            await db.rollback()
    finally:
        await engine.dispose()
