"""Atomic QAPE admission/compensation. Caller MUST check PostgreSQL before compensating.

All keys for a scope use one hash tag; do not mix keys from different scopes.
Reservation evidence TTL is explicitly >= recovery horizon; expiry is UNKNOWN,
never evidence that a request was not admitted.
"""
import math
from dataclasses import dataclass
from app.core.redis import get_redis
from app.services.qape_rate_limiter import _digest


ADMIT_LUA = """
local bucket, window, reservation = KEYS[1], KEYS[2], KEYS[3]
local cap, refill, length, maxreq, ttl = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]), tonumber(ARGV[4]), tonumber(ARGV[5])
local member = ARGV[6]
local cooldown = tonumber(ARGV[7]) or 0
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local previous = redis.call('HMGET', reservation, 'state', 'retry_until')
if previous[1] then
    if previous[1] == 'ACTIVE' then return {1, '0', previous[1]} end
    if previous[1] == 'DENIED' then
        local until_time = tonumber(previous[2])
        if not until_time then return {0, '0', 'UNKNOWN'} end
        return {0, tostring(math.max(0, until_time - now)), 'DENIED'}
    end
    return {0, '0', previous[1]}
end
local vals = redis.call('HMGET', bucket, 'tokens', 'updated')
local tokens = math.min(cap, (tonumber(vals[1]) or cap) + math.max(0, now - (tonumber(vals[2]) or now)) / refill)
redis.call('ZREMRANGEBYSCORE', window, '-inf', now - length)
local count = redis.call('ZCARD', window)
local retry = 0
if tokens < 1 then retry = (1 - tokens) * refill end
if count >= maxreq then
    local first = redis.call('ZRANGE', window, 0, 0, 'WITHSCORES')
    retry = math.max(retry, tonumber(first[2]) + length - now)
end
if retry > 0 then
    retry = math.max(retry, cooldown)
    redis.call('HSET', reservation, 'state', 'DENIED', 'retry_until', tostring(now + retry))
    redis.call('EXPIRE', reservation, ttl)
    return {0, tostring(retry), 'DENIED'}
end
redis.call('HSET', bucket, 'tokens', tostring(tokens - 1), 'updated', tostring(now))
redis.call('EXPIRE', bucket, math.max(ttl, math.ceil(cap * refill) + 1))
redis.call('ZADD', window, now, member)
redis.call('EXPIRE', window, math.max(ttl, math.ceil(length) + 1))
redis.call('HSET', reservation, 'state', 'ACTIVE', 'retry', '0', 'refill', tostring(refill))
redis.call('EXPIRE', reservation, ttl)
return {1, '0', 'ACTIVE'}
"""

PROBE_LUA = """
local values = redis.call('HMGET', KEYS[1], 'state', 'retry_until')
local state = values[1]
if not state then return {'UNKNOWN', '0', '0'} end
if state == 'ACTIVE' or state == 'COMPENSATED' then return {state, '0', '0'} end
if state ~= 'DENIED' then return {'UNKNOWN', '0', '0'} end
local deadline = tonumber(values[2])
if not deadline then return {'UNKNOWN', '0', '0'} end
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
return {'DENIED', tostring(math.max(0, deadline - now)), tostring(deadline)}
"""


COMPENSATE_LUA = """
local bucket, window, reservation = KEYS[1], KEYS[2], KEYS[3]
local state = redis.call('HGET', reservation, 'state')
if not state then return 'UNKNOWN' end
if state == 'COMPENSATED' then return 'COMPENSATED' end
if state ~= 'ACTIVE' then return 'NOT_ACTIVE' end
local cap = tonumber(ARGV[1])
local refill = tonumber(redis.call('HGET', reservation, 'refill'))
local vals = redis.call('HMGET', bucket, 'tokens', 'updated')
local tokens, updated = tonumber(vals[1]), tonumber(vals[2])
if not tokens or not updated or not refill or refill <= 0 then return 'UNKNOWN' end
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local natural = math.min(cap, tokens + math.max(0, now - updated) / refill)
redis.call('HSET', bucket, 'tokens', tostring(math.min(cap, natural + 1)), 'updated', tostring(now))
redis.call('EXPIRE', bucket, math.max(tonumber(ARGV[3]), math.ceil(cap * refill) + 1))
redis.call('ZREM', window, ARGV[2])
redis.call('HSET', reservation, 'state', 'COMPENSATED')
redis.call('EXPIRE', reservation, tonumber(ARGV[3]))
return 'COMPENSATED'
"""


@dataclass(frozen=True)
class ReservationResult:
    allowed: bool
    retry_after_seconds: float
    state: str


def _keys(org_id: str, service_id: str, user_id: str, reservation_id: str, generation: int):
    if generation < 0 or isinstance(generation, bool):
        raise ValueError('generation must be nonnegative')
    scope = ':'.join(_digest(s) for s in (org_id, service_id, user_id))
    tag = '{' + scope + '}'
    prefix = 'tonde:qape:v2:' + tag + ':'
    member = _digest(reservation_id + ':' + str(generation))
    return (prefix + 'bucket', prefix + 'window', prefix + 'reservation:' + member), member


async def reserve(*, org_id: str, service_id: str, user_id: str, reservation_id: str,
                  attempt_generation: int, capacity: int, refill_seconds: float,
                  window_seconds: float, max_requests: int, evidence_seconds: int = 86400,
                  cooldown_seconds: float = 0) -> ReservationResult:
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in (capacity, max_requests, evidence_seconds)):
        raise ValueError('capacity, max_requests and evidence_seconds must be positive integers')
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in (refill_seconds, window_seconds)):
        raise ValueError('refill and window must be positive finite numbers')
    if not isinstance(cooldown_seconds, (int, float)) or not math.isfinite(cooldown_seconds) or cooldown_seconds < 0:
        raise ValueError('cooldown_seconds must be nonnegative and finite')
    keys, member = _keys(org_id, service_id, user_id, reservation_id, attempt_generation)
    result = await (await get_redis()).eval(ADMIT_LUA, 3, *keys, capacity, refill_seconds,
                                             window_seconds, max_requests, evidence_seconds, member, cooldown_seconds)
    if result[2] not in ('ACTIVE', 'DENIED'):
        raise RuntimeError('Redis reservation evidence is unknown')
    return ReservationResult(bool(int(result[0])), float(result[1]), result[2])


async def reservation_state(*, org_id: str, service_id: str, user_id: str,
                            reservation_id: str, attempt_generation: int) -> tuple[str, float, float]:
    """Atomic Redis evidence read; missing/legacy/expired/malformed => UNKNOWN.

    The result never authorizes compensation without a fenced PostgreSQL check.
    """
    keys, _ = _keys(org_id, service_id, user_id, reservation_id, attempt_generation)
    result = await (await get_redis()).eval(PROBE_LUA, 1, keys[2])
    return result[0], float(result[1]), float(result[2])


async def compensate(*, org_id: str, service_id: str, user_id: str, reservation_id: str,
                     attempt_generation: int, capacity: int, evidence_seconds: int = 86400) -> str:
    """Low-level primitive; NOT an authorization to reimburse. Verify durable state first."""
    if capacity < 1 or evidence_seconds < 1:
        raise ValueError('positive capacity and evidence TTL required')
    keys, member = _keys(org_id, service_id, user_id, reservation_id, attempt_generation)
    return await (await get_redis()).eval(COMPENSATE_LUA, 3, *keys, capacity, member, evidence_seconds)
