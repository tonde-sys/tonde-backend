"""QAPE policy versions, user restrictions and audit events.

Revision ID: 015_qape_data
Revises: 014_event_outbox
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "015_qape_data"
down_revision = "014_event_outbox"
branch_labels = None
depends_on = None


def upgrade():
    dialect = op.get_bind().dialect.name
    document = JSONB() if dialect == "postgresql" else sa.JSON()
    # Required for composite tenant FKs. NULL org_id legacy services cannot be
    # targeted by QAPE service-scoped rows.
    op.create_unique_constraint("uq_services_id_org_qape", "services", ["id", "org_id"])
    op.create_table("abuse_policies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_id", sa.String(36), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config", document, nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["service_id", "org_id"], ["services.id", "services.org_id"], name="fk_abuse_policy_service_org"),
        sa.CheckConstraint("version >= 1", name="ck_abuse_policy_version"))
    op.create_index("uq_abuse_policy_org_version", "abuse_policies", ["org_id", "version"], unique=True,
                    postgresql_where=sa.text("service_id IS NULL"), sqlite_where=sa.text("service_id IS NULL"))
    op.create_index("uq_abuse_policy_service_version", "abuse_policies", ["service_id", "version"], unique=True,
                    postgresql_where=sa.text("service_id IS NOT NULL"), sqlite_where=sa.text("service_id IS NOT NULL"))
    op.create_index("uq_abuse_policy_active_org", "abuse_policies", ["org_id"], unique=True,
                    postgresql_where=sa.text("service_id IS NULL AND enabled"), sqlite_where=sa.text("service_id IS NULL AND enabled = 1"))
    op.create_index("uq_abuse_policy_active_service", "abuse_policies", ["service_id"], unique=True,
                    postgresql_where=sa.text("service_id IS NOT NULL AND enabled"), sqlite_where=sa.text("service_id IS NOT NULL AND enabled = 1"))

    op.create_table("abuse_restrictions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_id", sa.String(36)),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("restriction_type", sa.String(40), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["service_id", "org_id"], ["services.id", "services.org_id"], name="fk_abuse_restriction_service_org"),
        sa.CheckConstraint("expires_at > starts_at", name="ck_abuse_restriction_dates"))
    op.create_index("ix_abuse_restriction_lookup", "abuse_restrictions", ["org_id", "user_id", "service_id", "expires_at"])
    op.create_index("ix_abuse_restriction_active", "abuse_restrictions", ["org_id", "expires_at", "revoked_at"])

    op.create_table("abuse_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_id", sa.String(36)),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("ticket_id", sa.String(36), sa.ForeignKey("tickets.id", ondelete="RESTRICT")),
        sa.Column("device_id", sa.String(64)),
        sa.Column("kiosk_id", sa.String(36)),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("decision", sa.String(40)),
        sa.Column("reason_codes", document, nullable=False),
        sa.Column("policy_version", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata", document, nullable=False),
        sa.ForeignKeyConstraint(["service_id", "org_id"], ["services.id", "services.org_id"], name="fk_abuse_event_service_org"),
        sa.CheckConstraint("event_type <> 'TICKET_CREATED' OR (ticket_id IS NOT NULL AND service_id IS NOT NULL AND user_id IS NOT NULL)", name="ck_abuse_created_ticket"))
    op.create_index("ix_abuse_event_scope_time", "abuse_events", ["org_id", "service_id", "created_at"])
    op.create_index("ix_abuse_event_user_time", "abuse_events", ["org_id", "user_id", "created_at"])
    op.create_index("uq_abuse_ticket_created", "abuse_events", ["ticket_id"], unique=True,
                    postgresql_where=sa.text("event_type = 'TICKET_CREATED' AND ticket_id IS NOT NULL"),
                    sqlite_where=sa.text("event_type = 'TICKET_CREATED' AND ticket_id IS NOT NULL"))


def downgrade():
    op.drop_table("abuse_events")
    op.drop_table("abuse_restrictions")
    op.drop_table("abuse_policies")
    op.drop_constraint("uq_services_id_org_qape", "services", type_="unique")
