"""outbox des événements temps réel

Revision ID: 014_event_outbox
Revises: 013_queue_sequence
Create Date: 2026-11-16

Spécification Queue Engine v1.0, §6.3.

`publish_event` diffuse APRÈS le commit et avale ses erreurs : une panne Redis
au mauvais moment perd l'événement. La transition est sûre en base, mais l'écran
de salle ne voit jamais l'appel avant de se resynchroniser.

Cette table écrit l'événement dans la MÊME transaction que le changement d'état.
Il devient impossible d'avoir l'un sans l'autre. La diffusion immédiate reste
tentée (latence inchangée) ; en cas d'échec, le répartiteur du scheduler rejoue.

`published_at IS NULL` est le prédicat du répartiteur, d'où l'index
`(published_at, created_at)` : sans lui, chaque passage balaie toute la table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "014_event_outbox"
down_revision: Union[str, Sequence[str], None] = "013_queue_sequence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "event_outbox",
        sa.Column("id", sa.String(length=36), primary_key=True),
        # NULL = canal public (écrans de salle non authentifiés).
        sa.Column("org_id", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(length=500), nullable=True),
    )
    op.create_index("ix_event_outbox_org_id", "event_outbox", ["org_id"])
    op.create_index("ix_event_outbox_pending", "event_outbox", ["published_at", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_event_outbox_pending", table_name="event_outbox")
    op.drop_index("ix_event_outbox_org_id", table_name="event_outbox")
    op.drop_table("event_outbox")
