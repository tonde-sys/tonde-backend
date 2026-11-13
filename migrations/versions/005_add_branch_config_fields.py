"""Add config fields to branches table — S2-10

Revision ID: 005_add_branch_config_fields
Revises: 004_add_user_organizations
Create Date: 2026-06-26 00:00:00.000000

Ajoute les champs de configuration de la file d'attente sur la table branches :
  - max_wait_minutes_alert  : seuil d'alerte temps d'attente (défaut : 30 min)
  - operating_hours         : horaires détaillés par jour (JSON nullable)
  - enable_sms_reminders    : activer les rappels SMS (défaut : false)
  - reminder_interval_minutes : intervalle rappel SMS (défaut : 10 min)
  - supported_languages     : langues supportées (JSON, défaut : ["fr"])
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = '005_add_branch_config_fields'
down_revision: Union[str, None] = '004_add_user_organizations'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Seuil d'alerte temps d'attente (minutes)
    op.add_column(
        'branches',
        sa.Column(
            'max_wait_minutes_alert',
            sa.Integer(),
            nullable=False,
            server_default='30',
        ),
    )

    # Horaires détaillés par jour — JSON nullable
    # ex: {"monday": {"open": "08:00", "close": "17:00"}, ...}
    op.add_column(
        'branches',
        sa.Column('operating_hours', sa.JSON(), nullable=True),
    )

    # Activer les rappels SMS avant le tour du client
    op.add_column(
        'branches',
        sa.Column(
            'enable_sms_reminders',
            sa.Boolean(),
            nullable=False,
            server_default='false',
        ),
    )

    # Intervalle de rappel SMS en minutes
    op.add_column(
        'branches',
        sa.Column(
            'reminder_interval_minutes',
            sa.Integer(),
            nullable=False,
            server_default='10',
        ),
    )

    # Langues supportées — JSON array
    # ex: ["fr", "en", "sw"]
    op.add_column(
        'branches',
        sa.Column(
            'supported_languages',
            sa.JSON(),
            nullable=True,
            server_default=sa.text("""'["fr"]'"""),
        ),
    )


def downgrade() -> None:
    op.drop_column('branches', 'supported_languages')
    op.drop_column('branches', 'reminder_interval_minutes')
    op.drop_column('branches', 'enable_sms_reminders')
    op.drop_column('branches', 'operating_hours')
    op.drop_column('branches', 'max_wait_minutes_alert')
