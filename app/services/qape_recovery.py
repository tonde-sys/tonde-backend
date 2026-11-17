"""Conservative recovery: never reimburse until durable absence of a ticket is proven.

No automatic review authorization. The caller schedules this worker and owns alerts.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.qape import AbuseEvent
from app.models.qape_operation import OperationState, QapeCreateOperation
from app.models.ticket import Ticket
from app.models.queue_log import QueueLog, QueueLogAction
from app.models.event_outbox import EventOutbox
from app.services.qape_reservation import compensate, reservation_state
from app.services.qape_engine import QapeDecisionEngine, Decision, DecisionCode

logger = logging.getLogger(__name__)


async def claim_expired(db: AsyncSession, *, owner: str, limit: int = 50,
                        lease_seconds: int = 60, operation_id: str | None = None) -> list[tuple[str, int]]:
    """SKIP LOCKED + monotonically increased generation fences other workers.

    The claim MUST be committed before external Redis operations are attempted.
    """
    now = datetime.now(timezone.utc)
    rows = (await db.scalars(select(QapeCreateOperation).where(
        (QapeCreateOperation.id == operation_id) if operation_id else True,
        QapeCreateOperation.state.in_([OperationState.PENDING.value, OperationState.RESERVED.value,
                                       OperationState.COMMITTING.value,
                                       OperationState.COMPENSATING.value,
                                       OperationState.RECOVERY_REQUIRED.value]),
        or_(QapeCreateOperation.lease_expires_at < now,
            and_(QapeCreateOperation.state.in_([OperationState.PENDING.value,
                                                OperationState.RESERVED.value]),
                 QapeCreateOperation.lease_expires_at.is_(None),
                 QapeCreateOperation.decision_code.is_distinct_from('READY_TO_RETRY'),
                 QapeCreateOperation.created_at < now - timedelta(seconds=30))),
    ).order_by(QapeCreateOperation.lease_expires_at).limit(limit).with_for_update(skip_locked=True))).all()
    claims = []
    for row in rows:
        row.lease_owner = owner
        row.lease_expires_at = now + timedelta(seconds=lease_seconds)
        # Fence PostgreSQL writers without changing the Redis reservation identity.
        row.lease_generation += 1
        claims.append((row.id, row.lease_generation))
    await db.commit()
    return claims


async def reconcile_one(db: AsyncSession, operation_id: str, owner: str, generation: int) -> str:
    """Re-check row lock and evidence. Unknown Redis evidence => manual recovery.

    Caller must guarantee a fresh AsyncSession per concurrent worker. No negative
    conclusion is drawn while an unexpired COMMITTING lease can still commit.
    """
    row = (await db.scalars(select(QapeCreateOperation).where(
        QapeCreateOperation.id == operation_id).with_for_update())).first()
    if row is None or row.lease_owner != owner or row.lease_generation != generation:
        await db.rollback()
        return 'FENCED'
    ticket = await db.get(Ticket, row.ticket_id) if row.ticket_id else None
    if not ticket:
        ticket = (await db.scalars(select(Ticket).where(
            Ticket.user_id == row.user_id, Ticket.client_action_id == row.client_action_id))).first()
    if ticket:
        event = (await db.scalars(select(AbuseEvent).where(
            AbuseEvent.ticket_id == ticket.id, AbuseEvent.event_type == 'TICKET_CREATED'))).first()
        created_log = (await db.scalars(select(QueueLog.id).where(
            QueueLog.ticket_id == ticket.id, QueueLog.action == QueueLogAction.TICKET_CREATED))).first()
        outbox = (await db.scalars(select(EventOutbox.id).where(
            EventOutbox.org_id == ticket.org_id,
            EventOutbox.payload['ticket_id'].as_string() == ticket.id,
            EventOutbox.payload['qape_operation_id'].as_string() == row.id,
            EventOutbox.payload['reason'].as_string() == 'ticket_created',
        ))).first()
        if (not event or event.provenance != 'LIVE' or event.decision != 'ALLOW'
            or not created_log or not outbox):
            row.state = OperationState.RECOVERY_REQUIRED.value
            row.decision_code = 'INCONSISTENT_TICKET_EVENT'
            await db.commit()
            logger.error('QAPE reconciliation inconsistent operation %s', operation_id)
            return 'RECOVERY_REQUIRED'
        row.ticket_id = ticket.id
        row.state = OperationState.COMMITTED.value
        await db.commit()
        return 'COMMITTED'
    # The claim advanced the DB fence. Any writer that already held the row
    # committed before this locked SELECT; any later writer sees the new epoch
    # and must abort at lock_for_creation_commit. Thus absence is durable here.
    if row.reservation_id is None:
        if row.state == OperationState.PENDING.value and row.attempt_generation == 0:
            # Initial intent committed, but reservation identity was never
            # persisted: by ordering, Lua could not have been sent.
            row.lease_owner = None
            row.lease_expires_at = None
            row.decision_code = 'READY_TO_RETRY'
            await db.commit()
            return 'PENDING'
        if row.state == OperationState.RESERVED.value and row.decision_code == 'REDIS_UNAVAILABLE_BYPASS':
            # Explicit ALLOW path recorded no Redis reservation. The DB fence
            # proves no prior writer can commit after this check.
            row.state = OperationState.COMPENSATED.value
            row.decision_code = 'BYPASS_ABORTED_NO_RESERVATION'
            await db.commit()
            return 'COMPENSATED'
        row.state = OperationState.RECOVERY_REQUIRED.value
        row.decision_code = 'MISSING_RESERVATION_ID'
        await db.commit()
        return 'RECOVERY_REQUIRED'
    try:
        redis_state, remaining, retry_deadline = await reservation_state(
            org_id=row.org_id, service_id=row.service_id, user_id=row.user_id,
            reservation_id=row.reservation_id, attempt_generation=row.attempt_generation)
    except Exception:
        redis_state, remaining, retry_deadline = 'UNKNOWN', 0, 0
    if redis_state == 'DENIED':
        # Lua persisted an absolute retry deadline, but the Python process died
        # before recording the refusal in PostgreSQL. No counter was consumed.
        row.state = OperationState.DENIED.value
        row.decision_code = 'RATE_LIMITED'
        row.retry_after = datetime.fromtimestamp(retry_deadline, timezone.utc)
        row.response_status = 429
        row.response_snapshot = {'code': 'RATE_LIMITED', 'operation_id': row.id,
                                 'retry_after_seconds': remaining}
        await QapeDecisionEngine(db).record(row, Decision(
            DecisionCode.TEMPORARY_BLOCK, ('RATE_LIMITED',), row.retry_after))
        await db.commit()
        return 'DENIED'
    if redis_state == 'COMPENSATED':
        row.state = OperationState.COMPENSATED.value
        await db.commit()
        return 'COMPENSATED'
    if redis_state != 'ACTIVE':
        row.state = OperationState.RECOVERY_REQUIRED.value
        row.decision_code = 'RESERVATION_UNKNOWN'
        logger.error('QAPE reservation evidence unavailable operation=%s', operation_id)
        await db.commit()
        return 'RECOVERY_REQUIRED'
    if not row.reservation_capacity:
        row.state = OperationState.RECOVERY_REQUIRED.value
        row.decision_code = 'MISSING_RESERVATION_CAPACITY'
        await db.commit()
        return 'RECOVERY_REQUIRED'
    try:
        result = await compensate(org_id=row.org_id, service_id=row.service_id,
            user_id=row.user_id, reservation_id=row.reservation_id,
            attempt_generation=row.attempt_generation, capacity=row.reservation_capacity)
    except Exception:
        result = 'UNKNOWN'
    if result == 'COMPENSATED':
        row.state = OperationState.COMPENSATED.value
    else:
        row.state = OperationState.RECOVERY_REQUIRED.value
        row.decision_code = 'COMPENSATION_' + result
        logger.error('QAPE compensation uncertain operation=%s result=%s', operation_id, result)
    await db.commit()
    return row.state
