"""Real Redis Lua atomicity and reservation identity; requires QAPE_REDIS_TEST."""
import asyncio
import os
import uuid
import pytest
import pytest_asyncio
from app.services.qape_reservation import reserve, compensate, _keys
from app.core.redis import get_redis, close_redis


@pytest_asyncio.fixture(autouse=True)
async def redis_connection_lifecycle():
    yield
    await close_redis()


@pytest.mark.asyncio
async def test_combined_atomic_concurrent_and_compensation():
    if not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('real Redis required')
    scope = uuid.uuid4().hex
    base = dict(org_id=scope, service_id=scope, user_id=scope,
                capacity=2, refill_seconds=3600, window_seconds=600, max_requests=1,
                attempt_generation=0)
    results = await asyncio.gather(*(reserve(**base, reservation_id=str(i)) for i in range(12)))
    assert sum(r.allowed for r in results) == 1
    accepted = str(next(i for i, r in enumerate(results) if r.allowed))
    assert (await reserve(**base, reservation_id=accepted)).allowed
    keys, member = _keys(scope, scope, scope, accepted, 0)
    redis = await get_redis()
    assert int(await redis.zcard(keys[1])) == 1
    assert float(await redis.hget(keys[0], 'tokens')) == pytest.approx(1, abs=.01)
    assert await compensate(org_id=scope, service_id=scope, user_id=scope,
                            reservation_id=accepted, attempt_generation=0, capacity=2) == 'COMPENSATED'
    assert await compensate(org_id=scope, service_id=scope, user_id=scope,
                            reservation_id=accepted, attempt_generation=0, capacity=2) == 'COMPENSATED'
    assert float(await redis.hget(keys[0], 'tokens')) == pytest.approx(2, abs=.01)
    assert await redis.zcard(keys[1]) == 0


@pytest.mark.asyncio
async def test_bucket_denial_does_not_insert_window():
    if not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('real Redis required')
    scope = uuid.uuid4().hex
    base = dict(org_id=scope, service_id=scope, user_id=scope,
                capacity=1, refill_seconds=3600, window_seconds=600, max_requests=3,
                attempt_generation=0)
    assert (await reserve(**base, reservation_id='one')).allowed
    assert not (await reserve(**base, reservation_id='two')).allowed
    keys, _ = _keys(scope, scope, scope, 'one', 0)
    assert await (await get_redis()).zcard(keys[1]) == 1


@pytest.mark.asyncio
async def test_compensation_refills_before_refund_and_caps_balance():
    if not os.getenv('QAPE_REDIS_TEST'):
        pytest.skip('real Redis required')
    redis = await get_redis()
    for elapsed, expected in ((0, 3), (4, 3.5), (40, 4)):
        scope = uuid.uuid4().hex
        args = dict(org_id=scope, service_id=scope, user_id=scope,
                    capacity=4, refill_seconds=8, window_seconds=600,
                    max_requests=5, attempt_generation=0)
        assert (await reserve(**args, reservation_id='alpha')).allowed
        assert (await reserve(**args, reservation_id='beta')).allowed
        keys, _ = _keys(scope, scope, scope, 'alpha', 0)
        if elapsed:
            # Move the bucket timestamp back, not the reservation clock. Redis
            # TIME remains the authority inside the compensation Lua script.
            await redis.hincrbyfloat(keys[0], 'updated', -elapsed)
        result = await asyncio.gather(*(compensate(org_id=scope, service_id=scope,
            user_id=scope, reservation_id='alpha', attempt_generation=0, capacity=4)
            for _ in range(2)))
        assert result == ['COMPENSATED', 'COMPENSATED']
        tokens = float(await redis.hget(keys[0], 'tokens'))
        assert tokens == pytest.approx(expected, abs=.03)
        assert await redis.zcard(keys[1]) == 1  # beta remains untouched
        assert await compensate(org_id=scope, service_id=scope, user_id=scope,
            reservation_id='alpha', attempt_generation=0, capacity=4) == 'COMPENSATED'
        assert float(await redis.hget(keys[0], 'tokens')) == pytest.approx(tokens, abs=1e-9)
