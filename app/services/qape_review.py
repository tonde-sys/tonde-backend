"""Human review of a durable QAPE intent; never creates or authorizes a ticket.

Only the original client can replay its exact payload through TicketService after
approval. Every QAPE/TONDE check runs again; a continued Redis outage can review
again. The review decision itself is immutable and unique under the operation lock.
"""
import hashlib
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.access import assert_branch_access, ROLE_RANK
from app.models.qape import AbuseEvent
from app.models.qape_operation import QapeCreateOperation, OperationState
from app.models.user import User, UserRole


class QapeReviewService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _authorize(self, actor: User, org_id: str, agency_id: str) -> None:
        if not actor.is_active or ROLE_RANK[actor.role] < ROLE_RANK[UserRole.SUPERVISOR]:
            raise HTTPException(403, detail={'code': 'QAPE_REVIEW_FORBIDDEN'})
        await assert_branch_access(self.db, actor, org_id, agency_id)

    async def pending(self, actor: User, org_id: str, agency_id: str,
                      service_id: str | None, page: int, page_size: int) -> dict:
        await self._authorize(actor, org_id, agency_id)
        if page < 1 or not 1 <= page_size <= 100:
            raise HTTPException(422, detail={'code': 'INVALID_PAGINATION'})
        criteria = (QapeCreateOperation.org_id == org_id,
                    QapeCreateOperation.agency_id == agency_id,
                    QapeCreateOperation.state == OperationState.REVIEW_REQUIRED.value)
        if service_id is not None:
            criteria += (QapeCreateOperation.service_id == service_id,)
        total = await self.db.scalar(select(func.count(QapeCreateOperation.id)).where(*criteria)) or 0
        rows = (await self.db.scalars(select(QapeCreateOperation).where(*criteria)
            .order_by(QapeCreateOperation.created_at, QapeCreateOperation.id)
            .offset((page - 1) * page_size).limit(page_size))).all()
        # Do not expose hashes, snapshots, Redis reservation IDs or credentials.
        return {'total': total, 'page': page, 'page_size': page_size, 'items': [
            {'operation_id': row.id, 'user_id': row.user_id, 'org_id': row.org_id,
             'agency_id': row.agency_id, 'service_id': row.service_id,
             'reason_code': row.decision_code, 'review_generation': row.review_generation,
             'reason_codes': list(row.response_snapshot.get('reason_codes', []))
                 if isinstance(row.response_snapshot, dict) else [],
             'policy_version': row.policy_version, 'created_at': row.created_at}
            for row in rows]}

    async def decide(self, actor: User, org_id: str, operation_id: str,
                     action: str, reason: str, review_generation: int) -> dict:
        if not isinstance(reason, str):
            raise HTTPException(422, detail={'code': 'INVALID_REVIEW_DECISION'})
        reason = reason.strip()
        if action not in ('APPROVE', 'REJECT') or not 1 <= len(reason) <= 500:
            raise HTTPException(422, detail={'code': 'INVALID_REVIEW_DECISION'})
        # PostgreSQL row lock serializes staff decisions and client replay.
        row = (await self.db.scalars(select(QapeCreateOperation).where(
            QapeCreateOperation.id == operation_id,
            QapeCreateOperation.org_id == org_id).with_for_update()
            .execution_options(populate_existing=True))).first()
        if row is None:
            raise HTTPException(404, detail={'code': 'QAPE_OPERATION_NOT_FOUND'})
        await self._authorize(actor, org_id, row.agency_id)
        if actor.id == row.user_id:
            raise HTTPException(403, detail={'code': 'QAPE_SELF_REVIEW_FORBIDDEN'})
        if type(review_generation) is not int or review_generation < 1:
            raise HTTPException(422, detail={'code': 'INVALID_REVIEW_GENERATION'})
        if review_generation != row.review_generation:
            raise HTTPException(409, detail={'code': 'STALE_REVIEW_GENERATION'})
        # Legacy generation 1 keeps its original immutable audit identity.
        legacy_id = hashlib.sha256(f'qape-human-review:v1:{row.id}'.encode()).hexdigest()
        audit_id = hashlib.sha256(
            f'qape-human-review:v2:{row.id}:{review_generation}'.encode()).hexdigest()
        prior = (await self.db.scalars(select(AbuseEvent).where(
            AbuseEvent.org_id == org_id, AbuseEvent.event_type == 'QAPE_HUMAN_REVIEW',
            (AbuseEvent.request_id.in_([audit_id, legacy_id]) if review_generation == 1
             else AbuseEvent.request_id == audit_id)))).first()
        if prior is not None:
            if prior.decision != action or prior.details.get('actor_id') != actor.id or prior.details.get('reason') != reason:
                raise HTTPException(409, detail={'code': 'REVIEW_ALREADY_DECIDED'})
            return {'operation_id': row.id, 'decision': action, 'state': row.state,
                    'actor_id': actor.id, 'decided_at': prior.created_at,
                    'review_generation': review_generation}
        if row.state != OperationState.REVIEW_REQUIRED.value or row.reservation_id is not None:
            raise HTTPException(409, detail={'code': 'REVIEW_NOT_PENDING'})
        target = OperationState.PENDING if action == 'APPROVE' else OperationState.DENIED
        row.transition(target, human_authorized=True)
        if action == 'APPROVE':
            row.response_status = row.response_snapshot = row.decision_code = None
            row.retry_after = row.lease_owner = row.lease_expires_at = None
            row.reservation_deadline = row.reservation_capacity = None
        else:
            row.decision_code = 'HUMAN_REJECTED'
            row.retry_after = None  # no automatic DENIED -> PENDING transition
            row.response_status = 403
            row.response_snapshot = {'code': 'HUMAN_REJECTED', 'operation_id': row.id}
        now = datetime.now(timezone.utc)
        self.db.add(AbuseEvent(org_id=org_id, service_id=row.service_id,
            user_id=row.user_id, ticket_id=None, request_id=audit_id,
            event_type='QAPE_HUMAN_REVIEW', provenance='LIVE', decision=action,
            review_audit_version=2, review_operation_id=row.id,
            review_generation=review_generation,
            reason_codes=['HUMAN_APPROVED' if action == 'APPROVE' else 'HUMAN_REJECTED'],
            policy_version=row.policy_version, created_at=now,
            details={'operation_id': row.id, 'actor_id': actor.id, 'reason': reason,
                     'review_generation': review_generation}))
        await self.db.commit()
        return {'operation_id': row.id, 'decision': action, 'state': row.state,
                'actor_id': actor.id, 'decided_at': now,
                'review_generation': review_generation}
