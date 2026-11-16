"""absence et reprise : échéances persistées, compteur d'absences

Revision ID: 011_ticket_absence_recovery
Revises: 010_unique_active_service_prefix
Create Date: 2026-11-16

Spécification Queue Engine v1.0, §5.1. Jusqu'ici, l'algorithme d'absence
recalculait ses échéances à la volée depuis `called_at`, ce qui produisait deux
défauts mesurables :

  - la fenêtre de récupération partait de l'APPEL et non de l'ABSENCE, soit
    180 s d'écart sur chaque ticket ;
  - `called_at` est écrasé à chaque nouvel appel, donc une reprise suivie d'un
    nouvel appel repoussait silencieusement l'échéance d'expiration.

Aucun `missed_count` n'existait : un client pouvait revenir indéfiniment, à deux
SMS et 180 s de guichet par cycle.

Cette migration rend ces trois valeurs PERSISTÉES. Elle est purement additive :
toutes les colonnes sont nullables ou ont un défaut, donc les lignes existantes
restent valides et l'application peut être redéployée sans interruption.

`updated_at` est ajouté dans le même passage : il manquait, et l'audit d'un
ticket ne pouvait pas répondre à « quand cette ligne a-t-elle changé pour la
dernière fois ? ».

Index `(status, recovery_expires_at)` : c'est exactement le prédicat du worker
d'expiration (`WHERE status = 'absent' AND recovery_expires_at < now()`). Sans
lui, chaque passage balaie toute la table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "011_ticket_absence_recovery"
down_revision: Union[str, Sequence[str], None] = "010_unique_active_service_prefix"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column("grace_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("missed_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "tickets",
        sa.Column("last_missed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("recovery_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("requeued_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # Reprise de données : on ne PEUT PAS reconstituer `missed_count` (aucun
    # historique exploitable de façon fiable), mais on peut ancrer
    # `recovery_expires_at` des ABSENT en cours sur leur `called_at`, ce que
    # faisait le code précédent. Sans ce backfill, tous les ABSENT existants
    # auraient une échéance NULL et deviendraient IMMORTELS : le worker
    # d'expiration ne les verrait plus jamais.
    #
    # Les 30 minutes sont écrites en dur, et c'est volontaire : une migration
    # doit produire le même résultat quel que soit le `.env` du moment. 30 est
    # la valeur d'`ABSENT_RETURN_WINDOW_MINUTES` en vigueur au moment de la
    # migration. L'écart de 180 s avec la vraie sémantique (fenêtre ancrée sur
    # l'absence) est assumé : ces tickets ont été ouverts avant que le champ
    # existe, et les faire expirer plus tôt serait une régression visible.
    # Les migrations de ce dépôt ciblent PostgreSQL (voir la note de la 010 :
    # SQLite, utilisé par les tests et le dev local, construit son schéma depuis
    # les modèles et n'exécute pas les migrations). Cette garde évite qu'un
    # `alembic upgrade head` accidentel sur SQLite casse sur la syntaxe
    # `INTERVAL`, alors que les colonnes, elles, ont bien été ajoutées.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            UPDATE tickets
            SET recovery_expires_at = called_at + INTERVAL '30 minutes'
            WHERE status = 'absent'
              AND recovery_expires_at IS NULL
              AND called_at IS NOT NULL
            """
        )

    op.create_index(
        "ix_tickets_status_recovery",
        "tickets",
        ["status", "recovery_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_tickets_status_recovery", table_name="tickets")
    op.drop_column("tickets", "updated_at")
    op.drop_column("tickets", "requeued_at")
    op.drop_column("tickets", "recovery_expires_at")
    op.drop_column("tickets", "last_missed_at")
    op.drop_column("tickets", "missed_count")
    op.drop_column("tickets", "grace_expires_at")
