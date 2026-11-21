"""QAPE Phase 1A: atomic, tenant-scoped Redis rate limiting primitives.

No ticket creation or policy decisions are made here. Redis TIME is authoritative.
Keys contain SHA-256 digests of opaque identifiers, never raw identity data.
"""
import hashlib
import math
from dataclasses import dataclass
from enum import Enum

from app.core.redis import get_redis


class LimiterMechanism(str, Enum):
    TOKEN_BUCKET = "token_bucket"
    SLIDING_WINDOW = "sliding_window"


@dataclass(frozen=True)
class BucketResult:
    allowed: bool
    remaining_tokens: float
    retry_after_seconds: float
    mechanism: LimiterMechanism = LimiterMechanism.TOKEN_BUCKET


@dataclass(frozen=True)
class WindowResult:
    allowed: bool
    observed_requests: int
    retry_after_seconds: float
    mechanism: LimiterMechanism = LimiterMechanism.SLIDING_WINDOW


def _digest(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Identifiers must be non-empty strings")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _key(mechanism: str, institution_id: str, service_id: str, user_id: str) -> str:
    # Separate namespace from the Queue Engine. Hash each component to avoid
    # delimiter collisions and disclosure of identifiers in Redis key scans.
    return f"tonde:qape:v1:{mechanism}:{_digest(institution_id)}:{_digest(service_id)}:{_digest(user_id)}"


_BUCKET_LUA = """
local key = KEYS[1]
local cap = tonumber(ARGV[1])
local refill = tonumber(ARGV[2]) -- seconds per token
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local values = redis.call('HMGET', key, 'tokens', 'updated')
local tokens = tonumber(values[1]) or cap
local updated = tonumber(values[2]) or now
local elapsed = math.max(0, now - updated)
tokens = math.min(cap, tokens + elapsed / refill)
local allowed = 0
local retry = 0
if tokens + 0.000000000001 >= 1 then
    allowed = 1
    tokens = math.max(0, tokens - 1)
else
    retry = (1 - tokens) * refill
end
redis.call('HSET', key, 'tokens', tostring(tokens), 'updated', tostring(now))
redis.call('EXPIRE', key, math.ceil(cap * refill) + 1)
return {allowed, tostring(tokens), tostring(retry)}
"""


_WINDOW_LUA = """
local key = KEYS[1]
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local member = ARGV[3]
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
-- Window is (now-window, now]; an entry at the boundary is expired.
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local existing = redis.call('ZSCORE', key, member)
local count = redis.call('ZCARD', key)
if existing then
    redis.call('EXPIRE', key, math.ceil(window) + 1)
    return {1, count, '0'}
end
if count < limit then
    redis.call('ZADD', key, now, member)
    redis.call('EXPIRE', key, math.ceil(window) + 1)
    return {1, count + 1, '0'}
end
local earliest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local retry = tonumber(earliest[2]) + window - now
return {0, count, tostring(math.max(0, retry))}
"""


async def consume_token(*, institution_id: str, service_id: str, user_id: str,
                        capacity: int, refill_seconds: float) -> BucketResult:
    """Consume one token, atomically. refill_seconds is time per ONE token.

    Returns the current balance and seconds until the next token on denial.
    Redis errors are propagated: policy on failure belongs to the future engine.
    """
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
        raise ValueError("capacity must be a positive integer")
    if isinstance(refill_seconds, bool) or not isinstance(refill_seconds, (int, float)) or not math.isfinite(refill_seconds) or refill_seconds <= 0:
        raise ValueError("refill_seconds must be positive and finite")
    key = _key("bucket", institution_id, service_id, user_id)
    result = await (await get_redis()).eval(_BUCKET_LUA, 1, key, capacity, refill_seconds)
    return BucketResult(bool(int(result[0])), float(result[1]), float(result[2]))


async def record_request(*, institution_id: str, service_id: str, user_id: str,
                         request_id: str, window_seconds: float, max_requests: int) -> WindowResult:
    """Admit one distinct request into a sliding window, atomically.

    Replays of an admitted request are allowed without increasing the count.
    Denied requests are not recorded and must retry with the same request_id.
    request_id is hashed before storing it as the sorted-set member.
    """
    if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 1:
        raise ValueError("max_requests must be a positive integer")
    if isinstance(window_seconds, bool) or not isinstance(window_seconds, (int, float)) or not math.isfinite(window_seconds) or window_seconds <= 0:
        raise ValueError("window_seconds must be positive and finite")
    key = _key("window", institution_id, service_id, user_id)
    result = await (await get_redis()).eval(_WINDOW_LUA, 1, key, window_seconds, max_requests, _digest(request_id))
    return WindowResult(bool(int(result[0])), int(result[1]), float(result[2]))
