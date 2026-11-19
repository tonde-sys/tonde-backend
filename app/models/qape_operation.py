"""Durable QAPE creation intent. Never use audit events as mutable state.

Retention: operations and initial snapshots are retained indefinitely until an
explicit, reviewed retention policy exists. Never purge a reservation merely
because its Redis evidence TTL elapsed: Redis UNKNOWN requires manual recovery.
Snapshots contain only stable non-credential ticket fields, not the reusable
QR token, JWTs or OTPs. On replay the immutable QR credential is fetched from
the owned ticket row (not persisted a second time in the snapshot). The public
QR lookup is limited to settings.QR_TICKET_TTL_HOURS and exposes no private data.
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint, JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base


class OperationState(str, enum.Enum):
    PENDING = "PENDING"
    RESERVED = "RESERVED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    DENIED = "DENIED"
    COMMITTING = "COMMITTING"
    COMMITTED = "COMMITTED"
    COMPENSATING = "COMPENSATING"
    COMPENSATED = "COMPENSATED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


TRANSITIONS = {
    OperationState.PENDING: {OperationState.RESERVED, OperationState.REVIEW_REQUIRED, OperationState.DENIED, OperationState.RECOVERY_REQUIRED, OperationState.COMPENSATED},
    OperationState.RESERVED: {OperationState.COMMITTING, OperationState.COMMITTED, OperationState.COMPENSATING, OperationState.RECOVERY_REQUIRED, OperationState.DENIED},
    OperationState.COMMITTING: {OperationState.COMMITTED, OperationState.RECOVERY_REQUIRED},
    OperationState.COMPENSATING: {OperationState.COMPENSATED, OperationState.RECOVERY_REQUIRED},
    OperationState.DENIED: {OperationState.PENDING},
    OperationState.REVIEW_REQUIRED: {OperationState.PENDING, OperationState.DENIED},  # human approval or terminal rejection
    OperationState.RECOVERY_REQUIRED: {OperationState.COMMITTED, OperationState.COMPENSATING, OperationState.COMPENSATED},
    OperationState.COMMITTED: set(),
    OperationState.COMPENSATED: set(),
}


class QapeCreateOperation(Base):
    __tablename__ = "qape_create_operations"
    __table_args__ = (
        UniqueConstraint("user_id", "operation_type", "client_action_id", name="uq_qape_operation_intent"),
        UniqueConstraint("ticket_id", name="uq_qape_operation_ticket"),
        ForeignKeyConstraint(["agency_id", "org_id"], ["branches.id", "branches.org_id"], name="fk_qape_operation_agency_org"),
        ForeignKeyConstraint(["service_id", "org_id"], ["services.id", "services.org_id"], name="fk_qape_operation_service_org"),
        Index("ix_qape_operation_recovery", "state", "lease_expires_at"),
        CheckConstraint("attempt_generation >= 0 AND lease_generation >= 0", name="ck_qape_operation_generation"),
        CheckConstraint("state IN ('PENDING','RESERVED','REVIEW_REQUIRED','DENIED','COMMITTING','COMMITTED','COMPENSATING','COMPENSATED','RECOVERY_REQUIRED')", name="ck_qape_operation_state"),
        CheckConstraint("state <> 'COMMITTED' OR ticket_id IS NOT NULL", name="ck_qape_committed_ticket"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(40), nullable=False, default="CREATE_TICKET")
    client_action_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonicalization_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    org_id: Mapped[str] = mapped_column(String(36), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    agency_id: Mapped[str] = mapped_column(String(36), nullable=False)
    service_id: Mapped[str] = mapped_column(String(36), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default=OperationState.PENDING.value)
    ticket_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("tickets.id", ondelete="RESTRICT"))
    decision_code: Mapped[str | None] = mapped_column(String(80))
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    policy_version: Mapped[int | None] = mapped_column(Integer)
    policy_id: Mapped[str | None] = mapped_column(String(36), ForeignKey('abuse_policies.id', ondelete='RESTRICT'))
    reservation_id: Mapped[str | None] = mapped_column(String(64))
    reservation_capacity: Mapped[int | None] = mapped_column(Integer)
    attempt_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Separate DB fencing epoch: must not change the Redis reservation identity.
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reservation_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_snapshot: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def transition(self, target: OperationState, *, human_authorized: bool = False) -> None:
        current = OperationState(self.state)
        if target not in TRANSITIONS[current]:
            raise ValueError(f"Invalid QAPE transition: {current.value} -> {target.value}")
        if current == OperationState.REVIEW_REQUIRED and not human_authorized:
            raise ValueError("Human authorization required for a review transition")
        if current == OperationState.DENIED and target == OperationState.PENDING:
            due = self.retry_after
            if due is None or due.replace(tzinfo=due.tzinfo or timezone.utc) > datetime.now(timezone.utc):
                raise ValueError("Denial is not eligible for retry yet")
        self.state = target.value
