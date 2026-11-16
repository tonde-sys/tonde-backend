"""queue_sequence : ordre de file monotone, indépendant de l'horloge

Revision ID: 013_queue_sequence
Revises: 012_unique_active_ticket
Create Date: 2026-11-16

Spécification Queue Engine v1.0, §4.2.

Le score Redis était `(10 - priorité) × 10⁹ + created_ms`. Correct dans
l'immense majorité des cas, mais deux tickets créés dans la MÊME milliseconde
étaient départagés par l'ordre lexicographique de leur UUID : stable, mais
arbitraire. Pour le client qui voit passer son voisin, il n'y a pas
d'explication.

`queue_sequence` est un entier monotone, attribué par la base (séquence
PostgreSQL), qui devient la partie basse du score. Conséquences :

  - l'ordre ne dépend plus de l'horloge de l'application ;
  - deux tickets ne peuvent pas partager une séquence, donc il n'existe plus de
    cas d'égalité à départager ;
  - une reprise en file reçoit une nouvelle séquence, ce qui la place en fin de
    SA tranche de priorité — le comportement voulu, désormais formel.

Le backfill est déterministe : `ROW_NUMBER()` sur `(created_at, id)`, c'est-à-dire
exactement l'ordre que l'ancien score produisait. Aucune file n'est réordonnée
par cette migration — elle fige l'ordre existant sous une forme explicite.

La séquence PostgreSQL est calée au-dessus du maximum backfillé : sans cela, les
nouveaux tickets repartiraient de 1 et passeraient devant tout le monde.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "013_queue_sequence"
down_revision: Union[str, Sequence[str], None] = "012_unique_active_ticket"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SEQUENCE_NAME = "ticket_queue_sequence_seq"


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column("queue_sequence", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_index("ix_tickets_queue_sequence", "tickets", ["queue_sequence"])

    bind = op.get_bind()
    # SQLite construit son schéma depuis les modèles : il n'y a ni séquence ni
    # backfill à y faire, et les tests locaux ne voient donc jamais ce bloc.
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text(f"CREATE SEQUENCE IF NOT EXISTS {SEQUENCE_NAME}"))

        # Backfill : l'ordre exact de l'ancien score, figé explicitement.
        bind.execute(sa.text("""
            UPDATE tickets
            SET queue_sequence = ranked.rn
            FROM (
                SELECT id, ROW_NUMBER() OVER (ORDER BY created_at, id) AS rn
                FROM tickets
            ) ranked
            WHERE tickets.id = ranked.id
        """))

        # La séquence repart AU-DESSUS du backfill. `is_called = false` fait que
        # le prochain `nextval` renvoie exactement cette valeur.
        bind.execute(sa.text(
            f"SELECT setval('{SEQUENCE_NAME}', "
            f"(SELECT COALESCE(MAX(queue_sequence), 0) FROM tickets) + 1, false)"
        ))


def downgrade() -> None:
    op.drop_index("ix_tickets_queue_sequence", table_name="tickets")
    op.drop_column("tickets", "queue_sequence")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text(f"DROP SEQUENCE IF EXISTS {SEQUENCE_NAME}"))
