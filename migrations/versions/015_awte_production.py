"""Persistent AWTE configuration, observations, profiles and forecasts.

Revision ID: 015_awte_production
Revises: 014_event_outbox
"""
from datetime import datetime, timezone
import uuid
import statistics

from alembic import op
import sqlalchemy as sa


revision = "015_awte_production"
down_revision = "014_event_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("services", sa.Column("eta_ewma_alpha", sa.Float(), nullable=False, server_default="0.25"))
    op.add_column("services", sa.Column("eta_min_service_samples", sa.Integer(), nullable=False, server_default="3"))
    op.add_column("services", sa.Column("eta_min_counter_samples", sa.Integer(), nullable=False, server_default="10"))
    op.add_column("services", sa.Column("eta_outlier_min_samples", sa.Integer(), nullable=False, server_default="10"))
    op.add_column("services", sa.Column("eta_outlier_mad_multiplier", sa.Float(), nullable=False, server_default="3.0"))
    op.add_column("counters", sa.Column("is_paused", sa.Boolean(), nullable=False, server_default=sa.false()))

    op.create_table(
        "eta_observations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("branch_id", sa.String(36), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_id", sa.String(36), sa.ForeignKey("services.id", ondelete="CASCADE"), nullable=False),
        sa.Column("counter_id", sa.String(36), sa.ForeignKey("counters.id", ondelete="SET NULL"), nullable=True),
        sa.Column("ticket_id", sa.String(36), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_key", sa.String(120), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("exclusion_reason", sa.String(80), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("service_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("service_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("event_key", name="uq_eta_observation_event_key"),
    )
    op.create_index("ix_eta_observations_org_id", "eta_observations", ["org_id"])
    op.create_index("ix_eta_observations_branch_id", "eta_observations", ["branch_id"])
    op.create_index("ix_eta_observations_service_id", "eta_observations", ["service_id"])
    op.create_index("ix_eta_observations_ticket_id", "eta_observations", ["ticket_id"])
    op.create_index("ix_eta_observations_org_service_time", "eta_observations", ["org_id", "service_id", "observed_at"])
    op.create_index("ix_eta_observations_org_counter_time", "eta_observations", ["org_id", "counter_id", "observed_at"])

    op.create_table(
        "eta_service_profiles",
        sa.Column("service_id", sa.String(36), sa.ForeignKey("services.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ewma_seconds", sa.Float(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_eta_service_profiles_org_id", "eta_service_profiles", ["org_id"])

    op.create_table(
        "eta_counter_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_id", sa.String(36), sa.ForeignKey("services.id", ondelete="CASCADE"), nullable=False),
        sa.Column("counter_id", sa.String(36), sa.ForeignKey("counters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ewma_seconds", sa.Float(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "service_id", "counter_id", name="uq_eta_counter_profile_scope"),
    )
    op.create_index("ix_eta_counter_profiles_org_id", "eta_counter_profiles", ["org_id"])
    op.create_index("ix_eta_counter_profiles_service_id", "eta_counter_profiles", ["service_id"])
    op.create_index("ix_eta_counter_profiles_counter_id", "eta_counter_profiles", ["counter_id"])
    op.create_index("ix_eta_counter_profiles_org_service", "eta_counter_profiles", ["org_id", "service_id"])

    op.create_table(
        "eta_predictions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ticket_id", sa.String(36), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("branch_id", sa.String(36), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_id", sa.String(36), sa.ForeignKey("services.id", ondelete="CASCADE"), nullable=False),
        sa.Column("counter_id", sa.String(36), sa.ForeignKey("counters.id", ondelete="SET NULL"), nullable=True),
        sa.Column("estimated_wait_seconds", sa.Integer(), nullable=True),
        sa.Column("profile_level", sa.String(20), nullable=False),
        sa.Column("confidence", sa.String(20), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("actual_wait_seconds", sa.Integer(), nullable=True),
        sa.Column("absolute_error_seconds", sa.Integer(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("ticket_id", "version", name="uq_eta_prediction_ticket_version"),
    )
    op.create_index("ix_eta_predictions_org_id", "eta_predictions", ["org_id"])
    op.create_index("ix_eta_predictions_branch_id", "eta_predictions", ["branch_id"])
    op.create_index("ix_eta_predictions_service_id", "eta_predictions", ["service_id"])
    op.create_index("ix_eta_predictions_org_service_eval", "eta_predictions", ["org_id", "service_id", "evaluated_at"])
    op.create_index("ix_eta_predictions_org_service_counter_eval", "eta_predictions", ["org_id", "service_id", "counter_id", "evaluated_at"])
    op.create_index("ix_eta_predictions_org_service_ticket_version", "eta_predictions", ["org_id", "service_id", "ticket_id", "version"])

    _backfill_observations_and_profiles()


def _backfill_observations_and_profiles() -> None:
    """Use trustworthy legacy ticket timestamps to seed the new real-data model."""
    bind = op.get_bind()
    rows = bind.execute(sa.text("""
        SELECT t.id AS ticket_id, t.org_id, t.agency_id AS branch_id, t.service_id,
               c.id AS valid_counter_id, t.service_seconds, t.served_at, t.done_at, t.updated_at,
               s.avg_duration_minutes, s.eta_ewma_alpha, s.eta_outlier_min_samples,
               s.eta_outlier_mad_multiplier
        FROM tickets t
        JOIN branches b ON b.id = t.agency_id AND b.org_id = t.org_id
        JOIN services s ON s.id = t.service_id AND s.branch_id = b.id
            AND s.org_id = b.org_id
        LEFT JOIN counters c ON c.id = t.counter_id
            AND c.org_id = b.org_id AND c.agency_id = b.id
        WHERE t.status = 'DONE' AND t.org_id IS NOT NULL
        ORDER BY t.done_at ASC NULLS FIRST, t.id ASC
    """)).mappings().yield_per(1000)

    service_state: dict[str, dict] = {}
    counter_state: dict[tuple[str, str], dict] = {}
    eligible_history: dict[str, list[int]] = {}
    insert_observation = sa.text("""
        INSERT INTO eta_observations
            (id, org_id, branch_id, service_id, counter_id, ticket_id,
             event_key, duration_seconds, eligible, exclusion_reason, observed_at,
             service_started_at, service_completed_at)
        VALUES
            (:id, :org_id, :branch_id, :service_id, :counter_id, :ticket_id,
             :event_key, :duration_seconds, :eligible, :exclusion_reason, :observed_at,
             :started_at, :completed_at)
    """)
    observation_batch: list[dict] = []
    now = datetime.now(timezone.utc)
    for row in rows:
        started = row["served_at"]
        completed = row["done_at"]
        if started is not None and completed is not None and completed >= started:
            seconds = int((completed - started).total_seconds())
        else:
            seconds = row["service_seconds"]
        eligible = seconds is not None and seconds > 0 and started is not None and completed is not None and completed >= started
        reason = None if eligible else "missing_or_invalid_service_timestamps"
        history = eligible_history.setdefault(row["service_id"], [])
        if eligible:
            prior = history[-100:]
            if len(prior) >= row["eta_outlier_min_samples"]:
                center = statistics.median(prior)
                mad = statistics.median([abs(value - center) for value in prior])
                if mad > 0:
                    spread = row["eta_outlier_mad_multiplier"] * 1.4826 * mad
                    if seconds < max(0, center - spread) or seconds > center + spread:
                        eligible = False
                        reason = "robust_mad_outlier"
        observed_at = completed or row["updated_at"] or now
        observation_batch.append({
            "id": str(uuid.uuid4()), "org_id": row["org_id"], "branch_id": row["branch_id"],
            "service_id": row["service_id"], "counter_id": row["valid_counter_id"], "ticket_id": row["ticket_id"],
            "event_key": f"done:{row['ticket_id']}",
            "duration_seconds": seconds, "eligible": eligible, "exclusion_reason": reason,
            "observed_at": observed_at, "started_at": started, "completed_at": completed,
        })
        if len(observation_batch) == 1000:
            bind.execute(insert_observation, observation_batch)
            observation_batch.clear()
        if not eligible:
            continue

        history.append(seconds)
        if len(history) > 100:
            del history[:-100]

        service = service_state.setdefault(row["service_id"], {
            "org_id": row["org_id"], "ewma": float(row["avg_duration_minutes"] * 60),
            "count": 0, "alpha": float(row["eta_ewma_alpha"]),
        })
        service["ewma"] = service["alpha"] * seconds + (1 - service["alpha"]) * service["ewma"]
        service["count"] += 1
        if row["valid_counter_id"]:
            key = (row["service_id"], row["valid_counter_id"])
            counter = counter_state.setdefault(key, {
                "org_id": row["org_id"], "ewma": float(row["avg_duration_minutes"] * 60),
                "count": 0, "alpha": float(row["eta_ewma_alpha"]),
            })
            counter["ewma"] = counter["alpha"] * seconds + (1 - counter["alpha"]) * counter["ewma"]
            counter["count"] += 1

    if observation_batch:
        bind.execute(insert_observation, observation_batch)

    for service_id, profile in service_state.items():
        bind.execute(sa.text("""
            INSERT INTO eta_service_profiles (service_id, org_id, ewma_seconds, sample_count, updated_at)
            VALUES (:service_id, :org_id, :ewma, :count, :updated_at)
        """), {"service_id": service_id, "org_id": profile["org_id"], "ewma": profile["ewma"], "count": profile["count"], "updated_at": now})
    for (service_id, counter_id), profile in counter_state.items():
        bind.execute(sa.text("""
            INSERT INTO eta_counter_profiles (id, org_id, service_id, counter_id, ewma_seconds, sample_count, updated_at)
            VALUES (:id, :org_id, :service_id, :counter_id, :ewma, :count, :updated_at)
        """), {"id": str(uuid.uuid4()), "org_id": profile["org_id"], "service_id": service_id,
               "counter_id": counter_id, "ewma": profile["ewma"], "count": profile["count"], "updated_at": now})


def downgrade() -> None:
    op.drop_index("ix_eta_predictions_org_service_ticket_version", table_name="eta_predictions")
    op.drop_index("ix_eta_predictions_org_service_counter_eval", table_name="eta_predictions")
    op.drop_index("ix_eta_predictions_org_service_eval", table_name="eta_predictions")
    op.drop_index("ix_eta_predictions_service_id", table_name="eta_predictions")
    op.drop_index("ix_eta_predictions_branch_id", table_name="eta_predictions")
    op.drop_index("ix_eta_predictions_org_id", table_name="eta_predictions")
    op.drop_table("eta_predictions")
    op.drop_index("ix_eta_counter_profiles_org_service", table_name="eta_counter_profiles")
    op.drop_index("ix_eta_counter_profiles_counter_id", table_name="eta_counter_profiles")
    op.drop_index("ix_eta_counter_profiles_service_id", table_name="eta_counter_profiles")
    op.drop_index("ix_eta_counter_profiles_org_id", table_name="eta_counter_profiles")
    op.drop_table("eta_counter_profiles")
    op.drop_index("ix_eta_service_profiles_org_id", table_name="eta_service_profiles")
    op.drop_table("eta_service_profiles")
    op.drop_index("ix_eta_observations_org_counter_time", table_name="eta_observations")
    op.drop_index("ix_eta_observations_org_service_time", table_name="eta_observations")
    op.drop_index("ix_eta_observations_ticket_id", table_name="eta_observations")
    op.drop_index("ix_eta_observations_service_id", table_name="eta_observations")
    op.drop_index("ix_eta_observations_branch_id", table_name="eta_observations")
    op.drop_index("ix_eta_observations_org_id", table_name="eta_observations")
    op.drop_table("eta_observations")
    op.drop_column("counters", "is_paused")
    op.drop_column("services", "eta_outlier_mad_multiplier")
    op.drop_column("services", "eta_outlier_min_samples")
    op.drop_column("services", "eta_min_counter_samples")
    op.drop_column("services", "eta_min_service_samples")
    op.drop_column("services", "eta_ewma_alpha")
