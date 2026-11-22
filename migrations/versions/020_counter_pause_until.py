"""Expose the expected end of a counter pause to waiting clients.

Revision ID: 020_counter_pause_until
Revises: 019_awte_production
"""
from alembic import op
import sqlalchemy as sa


revision = "020_counter_pause_until"
down_revision = "019_awte_production"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "counters",
        sa.Column("paused_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("counters", "paused_until")
