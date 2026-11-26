"""Feedback: user reviews, bug reports and suggestions from the mobile app.

Revision ID: 021_feedback
Revises: 020_counter_pause_until

Note sur les libellés d'ENUM : SQLAlchemy persiste les NOMS des membres d'un
Enum Python ('REVIEW'), pas leurs valeurs ('review'). C'est l'inverse de ce que
les migrations 001 et 006 avaient fait, et la migration 007 a dû renommer tous
les libellés en majuscules pour rattraper le coup. Les deux types créés ici sont
donc écrits directement en majuscules : une base construite par Alembic et une
base construite par `create_all()` sont identiques.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "021_feedback"
down_revision = "020_counter_pause_until"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `create_type=False` est indispensable : sans lui, `create_table` réémet
    # `CREATE TYPE` pour chaque colonne et la migration échoue en
    # `DuplicateObjectError` juste après avoir créé le type. Le type est donc
    # créé UNE fois ici, explicitement, puis seulement référencé.
    category_enum = postgresql.ENUM(
        "REVIEW", "BUG", "SUGGESTION",
        name="feedbackcategory",
        create_type=False,
    )
    status_enum = postgresql.ENUM(
        "RECEIVED", "READ", "RESOLVED",
        name="feedbackstatus",
        create_type=False,
    )
    category_enum.create(op.get_bind(), checkfirst=True)
    status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "feedback",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column(
            "user_id", sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # SET NULL et non CASCADE : un retour déjà envoyé ne doit pas s'effacer
        # parce qu'un ticket a été purgé ou qu'une organisation a fermé.
        sa.Column(
            "org_id", sa.String(36),
            sa.ForeignKey("organizations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "ticket_id", sa.String(36),
            sa.ForeignKey("tickets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("category", category_enum, nullable=False),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("app_version", sa.String(32), nullable=True),
        sa.Column("platform", sa.String(16), nullable=True),
        sa.Column("device_model", sa.String(80), nullable=True),
        sa.Column("locale", sa.String(8), nullable=True),
        sa.Column(
            "status", status_enum, nullable=False, server_default="RECEIVED",
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )

    # La note n'a de sens qu'entre 1 et 5 : la contrainte est posée en base pour
    # qu'aucun chemin d'écriture (script, correction manuelle) ne la contourne.
    op.create_check_constraint(
        "ck_feedback_rating_range", "feedback",
        "rating IS NULL OR (rating >= 1 AND rating <= 5)",
    )
    # Un message vide n'est pas un retour ; 20 caractères est le minimum que
    # l'API impose, la base ne fait que garantir le plancher absolu.
    op.create_check_constraint(
        "ck_feedback_message_len", "feedback",
        "char_length(btrim(message)) >= 20",
    )

    op.create_index("ix_feedback_user_id", "feedback", ["user_id"])
    op.create_index("ix_feedback_org_id", "feedback", ["org_id"])
    op.create_index("ix_feedback_ticket_id", "feedback", ["ticket_id"])
    op.create_index("ix_feedback_category", "feedback", ["category"])
    op.create_index("ix_feedback_status", "feedback", ["status"])
    # « Mes retours », les plus récents d'abord, et le comptage du quota horaire
    # par utilisateur : les deux lisent (user_id, created_at).
    op.create_index("ix_feedback_user_created", "feedback", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_feedback_user_created", table_name="feedback")
    op.drop_index("ix_feedback_status", table_name="feedback")
    op.drop_index("ix_feedback_category", table_name="feedback")
    op.drop_index("ix_feedback_ticket_id", table_name="feedback")
    op.drop_index("ix_feedback_org_id", table_name="feedback")
    op.drop_index("ix_feedback_user_id", table_name="feedback")
    op.drop_table("feedback")
    sa.Enum(name="feedbackstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="feedbackcategory").drop(op.get_bind(), checkfirst=True)
