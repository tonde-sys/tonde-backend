"""unique ticket number per day

Revision ID: 007_unique_ticket_number
Revises: 006_add_notifications_and_queue_logs
Create Date: 2026-11-13

Ajoute l'index unique qui garantit qu'un numéro de ticket ne peut pas être
attribué deux fois le même jour dans la même agence.

Pourquoi un index d'expression : la séquence repart à 1 chaque jour, donc
(agency_id, prefix, sequence) seul n'est pas unique dans le temps. On ajoute
la date de création (UTC) comme quatrième composante, sans modifier le modèle.

Ceinture et bretelles : le compteur Redis atomique (next_daily_sequence)
élimine la course en amont ; cet index empêche toute duplication résiduelle
d'atteindre la base.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "007_unique_ticket_number"
down_revision: Union[str, Sequence[str], None] = "006_add_notifications_and_queue_logs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # L'index d'expression sur timestamptz est spécifique à PostgreSQL.
    # SQLite (tests, dev local) utilise Base.metadata.create_all.
    if bind.dialect.name != "postgresql":
        return

    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_tickets_number_daily
        ON tickets (
            agency_id,
            prefix,
            sequence,
            ((created_at AT TIME ZONE 'UTC')::date)
        )
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("DROP INDEX IF EXISTS uq_tickets_number_daily")
