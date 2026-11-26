"""Appareils de notification par utilisateur (B1bis).

Sans cette table, un second téléphone écrasait le jeton du premier : ses
notifications s'arrêtaient en silence, sans que personne ne l'ait demandé.

Revision ID: 023_user_devices
Revises: 022_org_support_contacts
"""
from alembic import op
import sqlalchemy as sa


revision = "023_user_devices"
down_revision = "022_org_support_contacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_devices",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token", sa.String(500), nullable=False),
        sa.Column("platform", sa.String(16), nullable=True),
        sa.Column("device_model", sa.String(80), nullable=True),
        sa.Column("app_version", sa.String(32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_user_devices_user_id", "user_devices", ["user_id"])
    # Unique : un jeton = une installation = un compte.
    op.create_index("ix_user_devices_token", "user_devices", ["token"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_user_devices_token", table_name="user_devices")
    op.drop_index("ix_user_devices_user_id", table_name="user_devices")
    op.drop_table("user_devices")
