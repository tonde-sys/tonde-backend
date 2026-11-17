"""QAPE Phase 1A: Lua atomicity and scope. Uses a real Redis when available."""
import asyncio
import uuid

import pytest
import pytest_asyncio
import redis.asyncio as redis

import app.services.qape_rate_limiter as limiter
from app.core.config import settings


@pytest_asyncio.fixture
async def qape_redis(monkeypatch):
    client = redis.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=1)
    try:
        await asyncio.wait_for(client.ping(), timeout=2)
    except Exception:
        await client.aclose()
        pytest.skip("Real Redis unavailable; Lua integration tests require Redis")
    # Preserve other tests' keys; clean up only QAPE keys created by this test.
    before = set(await client.keys("tonde:qape:v1:*"))
    monkeypatch.setattr(limiter, "get_redis", lambda: _client(client))
    try:
        yield client
    finally:
        created = set(await client.keys("tonde:qape:v1:*")) - before
        if created:
            await client.delete(*created)
        await client.aclose()


async def _client(client):
    return client


def ids(**overrides):
    return {"institution_id": "org-" + uuid.uuid4().hex,
            "service_id": "svc-" + uuid.uuid4().hex,
            "user_id": "user-" + uuid.uuid4().hex} | overrides


@pytest.mark.asyncio
async def test_bucket_initialization_exhaustion_refill_capacity_and_retry(qape_redis):
    scope = ids()
    first = await limiter.consume_token(**scope, capacity=2, refill_seconds=0.2)
    second = await limiter.consume_token(**scope, capacity=2, refill_seconds=0.2)
    denied = await limiter.consume_token(**scope, capacity=2, refill_seconds=0.2)
    assert first.allowed and 0.99 <= first.remaining_tokens <= 1
    assert second.allowed and second.remaining_tokens < 0.1
    assert not denied.allowed and 0 < denied.retry_after_seconds <= 0.2
    assert denied.mechanism == limiter.LimiterMechanism.TOKEN_BUCKET
    await asyncio.sleep(0.23)
    recovered = await limiter.consume_token(**scope, capacity=2, refill_seconds=0.2)
    assert recovered.allowed
    assert recovered.remaining_tokens < 0.5
    await asyncio.sleep(0.45)
    full = await limiter.consume_token(**scope, capacity=2, refill_seconds=0.2)
    assert full.allowed and 0.99 <= full.remaining_tokens <= 1


@pytest.mark.asyncio
async def test_bucket_concurrency(qape_redis):
    scope = ids()
    results = await asyncio.gather(*(limiter.consume_token(**scope, capacity=3, refill_seconds=60) for _ in range(25)))
    assert sum(result.allowed for result in results) == 3


@pytest.mark.asyncio
async def test_window_distinct_replay_expiration_and_retry(qape_redis):
    scope = ids()
    args = dict(**scope, window_seconds=0.35, max_requests=2)
    a = await limiter.record_request(**args, request_id="intent-a")
    b = await limiter.record_request(**args, request_id="intent-b")
    replay = await limiter.record_request(**args, request_id="intent-a")
    denied = await limiter.record_request(**args, request_id="intent-c")
    assert (a.observed_requests, b.observed_requests, replay.observed_requests) == (1, 2, 2)
    assert replay.allowed and not denied.allowed
    assert denied.observed_requests == 2 and 0 < denied.retry_after_seconds <= 0.35
    assert denied.mechanism == limiter.LimiterMechanism.SLIDING_WINDOW
    await asyncio.sleep(0.39)
    accepted = await limiter.record_request(**args, request_id="intent-c")
    assert accepted.allowed and accepted.observed_requests == 1


@pytest.mark.asyncio
async def test_window_is_truly_sliding(qape_redis):
    args = dict(**ids(), window_seconds=0.35, max_requests=2)
    await limiter.record_request(**args, request_id="one")
    await asyncio.sleep(0.19)
    await limiter.record_request(**args, request_id="two")
    await asyncio.sleep(0.19)
    third = await limiter.record_request(**args, request_id="three")
    assert third.allowed and third.observed_requests == 2
    fourth = await limiter.record_request(**args, request_id="four")
    assert not fourth.allowed and 0 < fourth.retry_after_seconds < 0.25


@pytest.mark.asyncio
async def test_window_concurrency(qape_redis):
    args = dict(**ids(), window_seconds=60, max_requests=4)
    results = await asyncio.gather(*(limiter.record_request(**args, request_id=str(i)) for i in range(30)))
    assert sum(result.allowed for result in results) == 4
    replay = await limiter.record_request(**args, request_id="0")
    # Arrival order is not guaranteed; replay one known accepted request instead.
    if not replay.allowed:
        admitted = next(i for i, result in enumerate(results) if result.allowed)
        replay = await limiter.record_request(**args, request_id=str(admitted))
    assert replay.allowed and replay.observed_requests == 4


@pytest.mark.asyncio
async def test_scopes_and_pseudonymization(qape_redis):
    scope = ids()
    changes = [scope, scope | {"user_id": "another-user"},
               scope | {"service_id": "another-service"},
               scope | {"institution_id": "another-organization"}]
    for s in changes:
        assert (await limiter.consume_token(**s, capacity=1, refill_seconds=60)).allowed
        assert (await limiter.record_request(**s, request_id="same-id", window_seconds=60, max_requests=1)).allowed
    for s in changes:
        assert not (await limiter.consume_token(**s, capacity=1, refill_seconds=60)).allowed
        assert not (await limiter.record_request(**s, request_id="different-id", window_seconds=60, max_requests=1)).allowed
    key = limiter._key("bucket", **scope)
    assert all(raw not in key for raw in scope.values())
    members = await qape_redis.zrange(limiter._key("window", **scope), 0, -1)
    assert members != ["same-id"]


@pytest.mark.asyncio
async def test_redis_error_is_not_converted_to_permission(monkeypatch):
    async def broken():
        raise ConnectionError("Redis down")
    monkeypatch.setattr(limiter, "get_redis", broken)
    scope = ids()
    with pytest.raises(ConnectionError):
        await limiter.consume_token(**scope, capacity=1, refill_seconds=10)
    with pytest.raises(ConnectionError):
        await limiter.record_request(**scope, request_id="x", window_seconds=10, max_requests=1)


def test_bad_configuration_and_ids():
    scope = ids()
    with pytest.raises(ValueError):
        limiter._key("bucket", "", scope["service_id"], scope["user_id"])
