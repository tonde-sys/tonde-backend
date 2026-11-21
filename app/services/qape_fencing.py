"""Database fencing check for a future QAPE final creation transaction.

Must be called on the SAME AsyncSession transaction that inserts ticket,
QueueLog, AbuseEvent, outbox and snapshot, before its one final commit.
"""
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.qape_operation import OperationState, QapeCreateOperation


class StaleOperation(RuntimeError):
    pass


async def lock_for_creation_commit_or_recovery(db: AsyncSession, operation_id: str,
                                               owner: str, lease_generation: int) -> QapeCreateOperation:
    row = (await db.scalars(select(QapeCreateOperation).where(
        QapeCreateOperation.id == operation_id).with_for_update())).one()
    if row.lease_owner != owner or row.lease_generation != lease_generation:
        raise StaleOperation('QAPE operation fenced by recovery worker')
    return row


async def lock_for_creation_commit(db: AsyncSession, *, operation_id: str,
                                   owner: str, lease_generation: int) -> QapeCreateOperation:
    row = (await db.scalars(select(QapeCreateOperation).where(
        QapeCreateOperation.id == operation_id).with_for_update())).one_or_none()
    now = datetime.now(timezone.utc)
    if (row is None or row.lease_owner != owner or
        row.lease_generation != lease_generation or
        row.state not in (OperationState.RESERVED.value, OperationState.COMMITTING.value) or
        row.lease_expires_at is None or row.lease_expires_at <= now):
        raise StaleOperation('QAPE lease lost; ticket creation must roll back')
    return row
