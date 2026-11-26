"""Canaux de support publiés par chaque institution (L6).

`phone` et `email` existaient déjà et servent de canaux de support : on
n'ajoute que ce qui manquait réellement — WhatsApp et les horaires.

Revision ID: 022_org_support_contacts
Revises: 021_feedback
"""
from alembic import op
import sqlalchemy as sa


revision = "022_org_support_contacts"
down_revision = "021_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("support_whatsapp", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("support_hours", sa.String(length=120), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organizations", "support_hours")
    op.drop_column("organizations", "support_whatsapp")
