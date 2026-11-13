"""unique ticket number per day

Revision ID: 008_unique_ticket_number
Revises: 007_queue_engine_completion
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
import logging
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

logger = logging.getLogger("alembic.runtime.migration")

# revision identifiers, used by Alembic.
revision: str = "008_unique_ticket_number"
down_revision: Union[str, Sequence[str], None] = "007_queue_engine_completion"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # L'index d'expression sur timestamptz est spécifique à PostgreSQL.
    # SQLite (tests, dev local) utilise Base.metadata.create_all.
    if bind.dialect.name != "postgresql":
        return

    # Une base existante peut déjà contenir des doublons (produits par l'ancien
    # COUNT+1). Créer l'index échouerait alors et ferait échouer TOUTE la
    # migration — donc le déploiement. On vérifie d'abord, et on n'échoue pas :
    # le compteur Redis atomique reste le garde-fou de production.
    duplicates = bind.execute(
        text(
            """
            SELECT count(*) FROM (
                SELECT 1
                FROM tickets
                GROUP BY agency_id, prefix, sequence, ((created_at AT TIME ZONE 'UTC')::date)
                HAVING count(*) > 1
            ) AS d
            """
        )
    ).scalar()

    if duplicates:
        logger.warning(
            "[008] %s numéro(s) de ticket en double détecté(s) : index unique NON créé. "
            "Le compteur Redis garantit l'unicité des nouveaux tickets. "
            "Pour activer la contrainte, renuméroter les doublons puis rejouer la migration.",
            duplicates,
        )
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
