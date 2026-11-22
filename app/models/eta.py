"""Persistent observations, EWMA profiles and current ETA forecasts."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class EtaObservation(Base):
    """One auditable service-duration observation per completed ticket."""

    __tablename__ = "eta_observations"
    __table_args__ = (
        UniqueConstraint("event_key", name="uq_eta_observation_event_key"),
        Index("ix_eta_observations_org_service_time", "org_id", "service_id", "observed_at"),
        Index("ix_eta_observations_org_counter_time", "org_id", "counter_id", "observed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String(36), ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[str] = mapped_column(String(36), ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    service_id: Mapped[str] = mapped_column(String(36), ForeignKey("services.id", ondelete="CASCADE"), index=True)
    counter_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("counters.id", ondelete="SET NULL"), nullable=True)
    ticket_id: Mapped[str] = mapped_column(String(36), ForeignKey("tickets.id", ondelete="CASCADE"))
    event_key: Mapped[str] = mapped_column(String(120), nullable=False)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    exclusion_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    service_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    service_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EtaServiceProfile(Base):
    """EWMA aggregate for one tenant-owned service."""

    __tablename__ = "eta_service_profiles"

    service_id: Mapped[str] = mapped_column(String(36), ForeignKey("services.id", ondelete="CASCADE"), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(36), ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    ewma_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class EtaCounterProfile(Base):
    """EWMA aggregate for one service at one physical counter."""

    __tablename__ = "eta_counter_profiles"
    __table_args__ = (
        UniqueConstraint("org_id", "service_id", "counter_id", name="uq_eta_counter_profile_scope"),
        Index("ix_eta_counter_profiles_org_service", "org_id", "service_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String(36), ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    service_id: Mapped[str] = mapped_column(String(36), ForeignKey("services.id", ondelete="CASCADE"), index=True)
    counter_id: Mapped[str] = mapped_column(String(36), ForeignKey("counters.id", ondelete="CASCADE"), index=True)
    ewma_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class EtaPrediction(Base):
    """One materially changed forecast version for a ticket."""

    __tablename__ = "eta_predictions"
    __table_args__ = (
        UniqueConstraint("ticket_id", "version", name="uq_eta_prediction_ticket_version"),
        Index("ix_eta_predictions_org_service_eval", "org_id", "service_id", "evaluated_at"),
        Index("ix_eta_predictions_org_service_counter_eval", "org_id", "service_id", "counter_id", "evaluated_at"),
        Index("ix_eta_predictions_org_service_ticket_version", "org_id", "service_id", "ticket_id", "version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ticket_id: Mapped[str] = mapped_column(String(36), ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    org_id: Mapped[str] = mapped_column(String(36), ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[str] = mapped_column(String(36), ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    service_id: Mapped[str] = mapped_column(String(36), ForeignKey("services.id", ondelete="CASCADE"), index=True)
    # Set from the ticket's actual assignment at first call for MAE breakdowns.
    counter_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("counters.id", ondelete="SET NULL"), nullable=True)
    estimated_wait_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    profile_level: Mapped[str] = mapped_column(String(20), nullable=False)
    confidence: Mapped[str] = mapped_column(String(20), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    actual_wait_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    absolute_error_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
