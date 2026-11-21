"""QAPE persistence access; no HTTP endpoints or rate-limiter calls."""
import hashlib
from datetime import datetime, timezone

from sqlalchemy import and_, or_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.branch import Service
from app.models.organization import Organization
from app.models.qape import AbuseEvent, AbusePolicy, AbuseRestriction
from app.models.ticket import Ticket
from app.models.user import User
from app.schemas.qape import EventInput, PolicyInput, RestrictionInput


class QapeScopeError(ValueError):
    pass


async def _check_scope(db: AsyncSession, org_id: str, service_id: str | None = None) -> None:
    if await db.get(Organization, org_id) is None:
        raise QapeScopeError("Organization not found")
    if service_id is not None:
        service = await db.get(Service, service_id)
        # Legacy services with NULL org_id cannot be assigned a policy implicitly.
        if service is None or service.org_id != org_id:
            raise QapeScopeError("Service does not belong to organization")


class PolicyRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, data: PolicyInput) -> AbusePolicy:
        await _check_scope(self.db, data.org_id, data.service_id)
        if data.created_by and await self.db.get(User, data.created_by) is None:
            raise QapeScopeError("Creator not found")
        row = AbusePolicy(**data.model_dump(exclude={"config"}), config=data.config.model_dump())
        self.db.add(row)
        await self.db.flush()  # caller controls commit and version replacement transaction
        return row

    async def versions(self, org_id: str, service_id: str | None = None) -> list[AbusePolicy]:
        scope = AbusePolicy.service_id == service_id if service_id else AbusePolicy.service_id.is_(None)
        return list((await self.db.scalars(select(AbusePolicy).where(AbusePolicy.org_id == org_id, scope)
                                           .order_by(AbusePolicy.version.desc()))).all())

    async def effective(self, org_id: str, service_id: str | None) -> AbusePolicy | None:
        await _check_scope(self.db, org_id, service_id)
        if service_id:
            row = (await self.db.scalars(select(AbusePolicy).where(
                AbusePolicy.org_id == org_id, AbusePolicy.service_id == service_id,
                AbusePolicy.enabled.is_(True)))).first()
            if row:
                return row
        return (await self.db.scalars(select(AbusePolicy).where(
            AbusePolicy.org_id == org_id, AbusePolicy.service_id.is_(None),
            AbusePolicy.enabled.is_(True)))).first()

    async def activate(self, org_id: str, policy_id: str) -> AbusePolicy:
        """Explicitly swap active version; caller commits or rolls back atomically."""
        row = await self.db.get(AbusePolicy, policy_id)
        if row is None or row.org_id != org_id:
            raise QapeScopeError("Policy not found in organization")
        # Flush deactivation BEFORE enabling the new row to satisfy partial unique indexes.
        scope = AbusePolicy.service_id == row.service_id if row.service_id is not None else AbusePolicy.service_id.is_(None)
        existing = (await self.db.scalars(select(AbusePolicy).where(
            AbusePolicy.org_id == org_id, scope, AbusePolicy.enabled.is_(True)))).all()
        for old in existing:
            old.enabled = False
        await self.db.flush()
        row.enabled = True
        await self.db.flush()
        return row


class RestrictionRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, data: RestrictionInput) -> AbuseRestriction:
        await _check_scope(self.db, data.org_id, data.service_id)
        if await self.db.get(User, data.user_id) is None:
            raise QapeScopeError("User not found")
        row = AbuseRestriction(**data.model_dump())
        self.db.add(row)
        await self.db.flush()
        return row

    async def applicable(self, org_id: str, user_id: str, service_id: str,
                         now: datetime | None = None) -> list[AbuseRestriction]:
        await _check_scope(self.db, org_id, service_id)
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("now must be timezone aware")
        query = select(AbuseRestriction).where(
            AbuseRestriction.org_id == org_id, AbuseRestriction.user_id == user_id,
            AbuseRestriction.service_id.in_([service_id]) | AbuseRestriction.service_id.is_(None),
            AbuseRestriction.starts_at <= now, AbuseRestriction.expires_at > now,
            AbuseRestriction.revoked_at.is_(None))
        return list((await self.db.scalars(query)).all())

    async def revoke(self, org_id: str, restriction_id: str, actor_id: str) -> AbuseRestriction:
        row = await self.db.get(AbuseRestriction, restriction_id)
        if row is None or row.org_id != org_id:
            raise QapeScopeError("Restriction not found in organization")
        if await self.db.get(User, actor_id) is None:
            raise QapeScopeError("Actor not found")
        if row.revoked_at is None:
            row.revoked_at = datetime.now(timezone.utc)
            row.revoked_by = actor_id
            await self.db.flush()
        return row


class EventRepository:
    """Append-only API; no update/delete methods. DB privileges must enforce this for other writers."""
    def __init__(self, db: AsyncSession):
        self.db = db

    async def append(self, data: EventInput) -> AbuseEvent:
        await _check_scope(self.db, data.org_id, data.service_id)
        if data.user_id and await self.db.get(User, data.user_id) is None:
            raise QapeScopeError("User not found")
        if data.ticket_id:
            ticket = await self.db.get(Ticket, data.ticket_id)
            if (ticket is None or ticket.org_id != data.org_id
                    or (data.user_id and ticket.user_id != data.user_id)
                    or (data.event_type == "TICKET_CREATED" and ticket.service_id != data.service_id)):
                raise QapeScopeError("Ticket does not belong to user/organization/service")
        # Audit stores only a digest of potentially user-supplied request IDs.
        row = AbuseEvent(**data.model_dump(exclude={"metadata", "request_id"}),
                         request_id=hashlib.sha256(data.request_id.encode("utf-8")).hexdigest(),
                         details=data.metadata)
        self.db.add(row)
        await self.db.flush()
        return row

    async def count_created(self, org_id: str, service_id: str, start_utc: datetime,
                            end_utc: datetime, user_id: str | None = None) -> int:
        """Count LIVE and HISTORICAL_CERTAIN creations in [start, end).

        HISTORICAL_INFERRED remains auditable but NEVER contributes to quota.
        Optional user_id scopes the count to the authenticated user without
        changing existing organization/service aggregate callers.
        Each ticket has at most one TICKET_CREATED event (partial unique index).
        Intervals are UTC boundaries of the organization's configured business day.
        """
        await _check_scope(self.db, org_id, service_id)
        if (start_utc.tzinfo is None or end_utc.tzinfo is None or start_utc >= end_utc):
            raise ValueError("Provide an increasing timezone-aware UTC interval")
        return int((await self.db.scalar(select(func.count(AbuseEvent.id)).where(
            AbuseEvent.org_id == org_id, AbuseEvent.service_id == service_id,
            AbuseEvent.user_id == user_id if user_id is not None else True,
            AbuseEvent.event_type == "TICKET_CREATED",
            or_(AbuseEvent.provenance == "HISTORICAL_CERTAIN",
                and_(AbuseEvent.provenance == "LIVE", AbuseEvent.decision == "ALLOW")),
            AbuseEvent.created_at >= start_utc, AbuseEvent.created_at < end_utc))) or 0)

    async def list(self, org_id: str, limit: int = 100, service_id: str | None = None) -> list[AbuseEvent]:
        if not 1 <= limit <= 500:
            raise ValueError("limit out of range")
        await _check_scope(self.db, org_id, service_id)
        query = select(AbuseEvent).where(AbuseEvent.org_id == org_id)
        if service_id:
            query = query.where(AbuseEvent.service_id == service_id)
        return list((await self.db.scalars(query.order_by(AbuseEvent.created_at.desc()).limit(limit))).all())
