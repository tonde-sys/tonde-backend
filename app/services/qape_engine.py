"""QAPE deterministic decision engine. PostgreSQL is authoritative.

Priorities: active restriction / daily quota / Redis hard denial > independent
soft review signals > allow. Preflight happens before consuming Redis; the
transactional daily-quota check in TicketService remains the final race guard.
No IP, device or shared kiosk is an individual abuse signal. In this release
there are no business-approved thresholds for cancellations, absences, prior
refusals, devices or kiosks: none are converted into automatic soft signals.
Trusted detectors may later provide *distinct* SoftSignal instances.
"""
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.qape import AbuseEvent, AbusePolicy
from app.models.qape_operation import QapeCreateOperation
from app.services.qape_data import EventRepository, PolicyRepository, RestrictionRepository
from app.services.qape_reservation import reserve


class DecisionCode(str, Enum):
    ALLOW = 'ALLOW'
    REVIEW_REQUIRED = 'REVIEW_REQUIRED'
    TEMPORARY_BLOCK = 'TEMPORARY_BLOCK'


@dataclass(frozen=True)
class SoftSignal:
    """Only server-verified, independent and policy-approved signals may enter."""
    code: str
    source: str


@dataclass(frozen=True)
class Decision:
    code: DecisionCode
    reason_codes: tuple[str, ...] = ()
    retry_after: datetime | None = None
    redis_reserved: bool = False


class QapeDecisionEngine:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def effective_policy(self, org_id: str, service_id: str) -> AbusePolicy | None:
        return await PolicyRepository(self.db).effective(org_id, service_id)

    async def preflight(self, *, policy: AbusePolicy, org_id: str, service_id: str,
                        user_id: str, now: datetime | None = None,
                        signals: tuple[SoftSignal, ...] = ()) -> Decision:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or policy.org_id != org_id or policy.service_id not in (None, service_id):
            raise ValueError('Invalid QAPE policy scope or server time')
        restrictions = await RestrictionRepository(self.db).applicable(org_id, user_id, service_id, now)
        if restrictions:
            # The earliest expiration at which a retry *might* succeed; other
            # simultaneous restrictions remain effective on a later attempt.
            earliest = min(row.expires_at.replace(tzinfo=row.expires_at.tzinfo or timezone.utc)
                           for row in restrictions)
            reasons = tuple(sorted({'ACTIVE_RESTRICTION', *(row.reason_code for row in restrictions)}))
            return Decision(DecisionCode.TEMPORARY_BLOCK, reasons, earliest)

        tz = ZoneInfo(policy.config['counting']['timezone'])
        local = now.astimezone(tz)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        start = midnight.astimezone(timezone.utc)
        end = (midnight + timedelta(days=1)).astimezone(timezone.utc)
        count = await EventRepository(self.db).count_created(
            org_id, service_id, start, end, user_id=user_id)
        if count >= policy.config['limits']['max_tickets_per_day']:
            return Decision(DecisionCode.TEMPORARY_BLOCK, ('DAILY_QUOTA_REACHED',), end)

        distinct = {signal.code for signal in signals if signal.code and signal.source}
        # There are currently no approved signal producers. This extension point
        # intentionally does not derive signals from raw QueueLog or shared IP.
        if len(distinct) >= policy.config['risk']['review_threshold']:
            return Decision(DecisionCode.REVIEW_REQUIRED,
                            tuple(sorted(distinct)))
        return Decision(DecisionCode.ALLOW)

    async def admit(self, *, policy: AbusePolicy, org_id: str, service_id: str,
                    user_id: str, reservation_id: str, attempt_generation: int) -> Decision:
        """Only place that calls the Phase 1C combined limiter. Uncertain EVAL
        outcomes are retried with the SAME ID, then propagated for recovery.
        """
        from app.core.redis import get_redis
        try:
            if not await (await get_redis()).ping():
                raise ConnectionError('Redis preflight PING failed')
        except Exception:
            mode = policy.config.get('redis_unavailable', 'REVIEW_REQUIRED')
            if mode == 'ALLOW':
                return Decision(DecisionCode.ALLOW, ('REDIS_UNAVAILABLE_BYPASS',))
            if mode == 'TEMPORARY_BLOCK':
                return Decision(DecisionCode.TEMPORARY_BLOCK, ('REDIS_UNAVAILABLE',),
                                datetime.now(timezone.utc) + timedelta(
                                    seconds=policy.config['risk']['cooldown_seconds']))
            return Decision(DecisionCode.REVIEW_REQUIRED, ('REDIS_UNAVAILABLE',))
        limits = policy.config['limits']
        arguments = dict(org_id=org_id, service_id=service_id, user_id=user_id,
                         reservation_id=reservation_id, attempt_generation=attempt_generation,
                         capacity=limits['bucket_capacity'],
                         refill_seconds=limits['bucket_refill_seconds'],
                         window_seconds=600,
                         max_requests=limits['max_requests_per_10m'],
                         cooldown_seconds=policy.config['risk']['cooldown_seconds'])
        try:
            result = await reserve(**arguments)
        except Exception:
            result = await reserve(**arguments)  # same identity, never fresh consumption
        if result.allowed:
            return Decision(DecisionCode.ALLOW, redis_reserved=True)
        retry = result.retry_after_seconds
        return Decision(DecisionCode.TEMPORARY_BLOCK, ('RATE_LIMITED',),
                        datetime.now(timezone.utc) + timedelta(seconds=retry))

    async def record(self, operation: QapeCreateOperation, decision: Decision) -> None:
        """Append at most one audit event per operation generation and decision.

        Must be called while holding the operation row lock; caller commits state
        and event together. Retries that merely replay never call record.
        """
        from sqlalchemy import select
        event_type = 'QAPE_DECISION'
        identity = hashlib.sha256(
            f'{operation.id}:{operation.attempt_generation}:{decision.code.value}'.encode()
        ).hexdigest()
        exists = (await self.db.scalars(select(AbuseEvent.id).where(
            AbuseEvent.org_id == operation.org_id,
            AbuseEvent.event_type == event_type,
            AbuseEvent.request_id == identity))).first()
        if exists:
            return
        self.db.add(AbuseEvent(org_id=operation.org_id, service_id=operation.service_id,
            user_id=operation.user_id, ticket_id=None, request_id=identity,
            event_type=event_type, provenance='LIVE', decision=decision.code.value,
            reason_codes=list(decision.reason_codes), policy_version=operation.policy_version,
            created_at=datetime.now(timezone.utc),
            details={'operation_id': operation.id, 'attempt_generation': operation.attempt_generation}))
        await self.db.flush()
