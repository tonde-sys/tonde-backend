"""QAPE persistent policies, temporary restrictions and append-only audit events."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint,
                        Index, Integer, JSON, String, UniqueConstraint, text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


def utcnow():
    return datetime.now(timezone.utc)


class AbusePolicy(Base):
    __tablename__ = "abuse_policies"
    __table_args__ = (
        ForeignKeyConstraint(["service_id", "org_id"], ["services.id", "services.org_id"],
                             name="fk_abuse_policy_service_org"),
        # NULL service scopes need their own unique index (NULL != NULL in SQL).
        Index("uq_abuse_policy_org_version", "org_id", "version", unique=True,
              postgresql_where=text("service_id IS NULL"), sqlite_where=text("service_id IS NULL")),
        Index("uq_abuse_policy_service_version", "service_id", "version", unique=True,
              postgresql_where=text("service_id IS NOT NULL"), sqlite_where=text("service_id IS NOT NULL")),
        Index("uq_abuse_policy_active_org", "org_id", unique=True,
              postgresql_where=text("service_id IS NULL AND enabled"),
              sqlite_where=text("service_id IS NULL AND enabled = 1")),
        Index("uq_abuse_policy_active_service", "service_id", unique=True,
              postgresql_where=text("service_id IS NOT NULL AND enabled"),
              sqlite_where=text("service_id IS NOT NULL AND enabled = 1")),
        CheckConstraint("version >= 1", name="ck_abuse_policy_version"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id: Mapped[str] = mapped_column(String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    service_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    config: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class AbuseRestriction(Base):
    __tablename__ = "abuse_restrictions"
    __table_args__ = (
        ForeignKeyConstraint(["service_id", "org_id"], ["services.id", "services.org_id"],
                             name="fk_abuse_restriction_service_org"),
        Index("ix_abuse_restriction_lookup", "org_id", "user_id", "service_id", "expires_at"),
        Index("ix_abuse_restriction_active", "org_id", "expires_at", "revoked_at"),
        CheckConstraint("expires_at > starts_at", name="ck_abuse_restriction_dates"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id: Mapped[str] = mapped_column(String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    service_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Phase 1B explicitly restricts individual users; organization-wide blocking
    # of all users is not implicit in a NULL user_id.
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    restriction_type: Mapped[str] = mapped_column(String(40), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AbuseEvent(Base):
    __tablename__ = "abuse_events"
    __table_args__ = (
        ForeignKeyConstraint(["service_id", "org_id"], ["services.id", "services.org_id"],
                             name="fk_abuse_event_service_org"),
        Index("ix_abuse_event_scope_time", "org_id", "service_id", "created_at"),
        Index("ix_abuse_event_user_time", "org_id", "user_id", "created_at"),
        Index("uq_qape_human_review_cycle", "review_operation_id", "review_generation", unique=True,
              postgresql_where=text("event_type = 'QAPE_HUMAN_REVIEW' AND review_audit_version = 2"),
              sqlite_where=text("event_type = 'QAPE_HUMAN_REVIEW' AND review_audit_version = 2")),
        CheckConstraint("(review_audit_version IS NULL AND review_operation_id IS NULL AND review_generation IS NULL) OR (event_type = 'QAPE_HUMAN_REVIEW' AND review_audit_version IS NOT NULL AND review_audit_version = 2 AND review_operation_id IS NOT NULL AND review_generation IS NOT NULL AND review_generation > 0 AND request_id IS NOT NULL)", name="ck_qape_review_v2_complete"),
        Index("uq_abuse_ticket_created", "ticket_id", unique=True,
              postgresql_where=text("event_type = 'TICKET_CREATED' AND ticket_id IS NOT NULL"),
              sqlite_where=text("event_type = 'TICKET_CREATED' AND ticket_id IS NOT NULL")),
        CheckConstraint("event_type <> 'TICKET_CREATED' OR (ticket_id IS NOT NULL AND service_id IS NOT NULL AND user_id IS NOT NULL)", name="ck_abuse_created_ticket"),
        CheckConstraint("provenance IN ('LIVE', 'HISTORICAL_CERTAIN', 'HISTORICAL_INFERRED')", name="ck_abuse_event_provenance"),
        CheckConstraint("provenance = 'LIVE' OR decision IS NULL", name="ck_abuse_historical_no_decision"),
        CheckConstraint("event_type <> 'TICKET_CREATED' OR provenance <> 'LIVE' OR (decision IS NOT NULL AND decision = 'ALLOW')",
                        name="ck_abuse_live_creation_allow"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id: Mapped[str] = mapped_column(String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    service_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    ticket_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("tickets.id", ondelete="RESTRICT"), nullable=True)
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # pseudonym only
    kiosk_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # no kiosk table yet
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    provenance: Mapped[str] = mapped_column(String(24), nullable=False, server_default="LIVE")
    decision: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reason_codes: Mapped[list] = mapped_column(JSON_DOCUMENT, nullable=False)
    policy_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    review_audit_version: Mapped[int | None] = mapped_column(Integer)
    review_operation_id: Mapped[str | None] = mapped_column(String(36))
    review_generation: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict] = mapped_column("metadata", JSON_DOCUMENT, nullable=False)
