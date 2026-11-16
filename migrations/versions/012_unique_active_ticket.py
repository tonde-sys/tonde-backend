"""un seul ticket actif par utilisateur, garanti par la base

Revision ID: 012_unique_active_ticket
Revises: 011_ticket_absence_recovery
Create Date: 2026-11-16

Spécification Queue Engine v1.0, §6.2 et §7.1.

Jusqu'ici l'invariant « un seul ticket actif par utilisateur » reposait sur deux
mécanismes applicatifs : un `SELECT` (`_get_active_ticket`) et un verrou Redis
par utilisateur. Deux conséquences mesurables :

  - deux instances pouvaient franchir le `SELECT` en même temps, et seule la
    fenêtre du verrou Redis les séparait ;
  - si Redis tombait, `acquire_ticket_creation_lock` levait et la création
    répondait `503 QUEUE_UNAVAILABLE`. L'invariant tenait, mais la
    disponibilité tombait avec Redis.

Un index unique PARTIEL rend l'invariant structurel : la base refuse elle-même
le second ticket actif, quelle que soit l'application ou le nombre d'instances.

Le prédicat porte sur les libellés d'énumération PostgreSQL, c'est-à-dire le NOM
du membre (`WAITING`) et non sa valeur (`waiting`) — même piège que la migration
011, où un backfill en minuscules avait cassé `alembic upgrade` sur une base
vide.

La reprise de données n'est pas optionnelle : l'index ne peut pas être créé si
des doublons existent, et il en existe forcément (rien ne les empêchait). On
garde le ticket actif LE PLUS RÉCENT de chaque utilisateur — c'est celui qu'il
est en train de vivre — et on annule les autres, en libérant les guichets qui
les servaient encore.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "012_unique_active_ticket"
down_revision: Union[str, Sequence[str], None] = "011_ticket_absence_recovery"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Statuts « actifs » : ils bloquent la création d'un nouveau ticket.
# MAJUSCULES = noms des membres de l'énumération PostgreSQL.
ACTIVE_PREDICATE = (
    "status IN ('WAITING', 'CALLED', 'SERVING', 'ABSENT', 'TRANSFERRED', 'INCOMPLETE')"
)


def upgrade() -> None:
    bind = op.get_bind()

    # ── Reprise de données (PostgreSQL uniquement) ────────────────────────────
    # SQLite construit son schéma depuis les modèles : la migration n'y touche
    # pas, et les tests locaux ne voient donc jamais ce bloc. C'est précisément
    # pourquoi il est isolé derrière un test qui rend le SQL hors ligne.
    if bind.dialect.name == "postgresql":
        # Un ticket actif par utilisateur : on garde le plus récent.
        # `id DESC` départage deux créations dans la même transaction.
        # Les deux requêtes partagent le même classement et s'exécutent dans la
        # même transaction : elles voient donc exactement les mêmes lignes.
        ranking = f"""
            SELECT id, ROW_NUMBER() OVER (
                       PARTITION BY user_id
                       ORDER BY created_at DESC, id DESC
                   ) AS rn
            FROM tickets
            WHERE {ACTIVE_PREDICATE}
        """

        # Libérer les guichets qui servaient un ticket sur le point d'être annulé.
        # Sans cela, le guichet resterait `current_ticket_id` sur un ticket clos
        # et refuserait tout nouvel appel (COUNTER_BUSY) jusqu'à la maintenance.
        bind.execute(sa.text(f"""
            UPDATE counters
            SET current_ticket_id = NULL
            WHERE current_ticket_id IN (SELECT id FROM ({ranking}) d WHERE d.rn > 1)
        """))

        bind.execute(sa.text(f"""
            UPDATE tickets
            SET status = 'CANCELLED',
                updated_at = now()
            WHERE id IN (SELECT id FROM ({ranking}) d WHERE d.rn > 1)
        """))

    # ── L'invariant ───────────────────────────────────────────────────────────
    op.create_index(
        "uq_tickets_active_per_user",
        "tickets",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_PREDICATE),
        sqlite_where=sa.text(ACTIVE_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index("uq_tickets_active_per_user", table_name="tickets")
    # Aucune donnée à restaurer : les tickets annulés par la reprise du upgrade
    # l'ont été en connaissance de cause (doublons d'un invariant violé). Les
    # ressusciter créerait de nouveau les doublons que l'index interdisait.
