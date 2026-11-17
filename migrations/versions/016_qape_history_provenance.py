"""Distinguish historical evidence from live QAPE decisions.

Revision ID: 016_qape_history_provenance
Revises: 015_qape_data
"""
from alembic import op
import sqlalchemy as sa

revision = "016_qape_history_provenance"
down_revision = "015_qape_data"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("abuse_events", sa.Column("provenance", sa.String(24), nullable=False,
                                            server_default="LIVE"))
    op.create_check_constraint("ck_abuse_event_provenance", "abuse_events",
                               "provenance IN ('LIVE', 'HISTORICAL_CERTAIN', 'HISTORICAL_INFERRED')")
    op.create_check_constraint("ck_abuse_historical_no_decision", "abuse_events",
                               "provenance = 'LIVE' OR decision IS NULL")
    op.create_check_constraint("ck_abuse_live_creation_allow", "abuse_events",
                               "event_type <> 'TICKET_CREATED' OR provenance <> 'LIVE' OR (decision IS NOT NULL AND decision = 'ALLOW')")


def downgrade():
    op.drop_constraint("ck_abuse_live_creation_allow", "abuse_events", type_="check")
    op.drop_constraint("ck_abuse_historical_no_decision", "abuse_events", type_="check")
    op.drop_constraint("ck_abuse_event_provenance", "abuse_events", type_="check")
    op.drop_column("abuse_events", "provenance")
