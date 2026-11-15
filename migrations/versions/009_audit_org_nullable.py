"""audit_logs.org_id nullable — journaliser les connexions hors organisation

Revision ID: 009_audit_org_nullable
Revises: 008_unique_ticket_number
Create Date: 2026-11-15

Constat P3-17 de l'audit : `audit_logs.org_id` était `NOT NULL` avec clé
étrangère. Or deux populations n'ont AUCUNE organisation :

  - `super_admin` (compte Tonde, transverse à toutes les organisations) ;
  - client OTP, qui n'appartient à aucune organisation.

Leurs connexions ne pouvaient donc pas être journalisées — précisément les deux
comptes les plus sensibles. Rendre la colonne nullable est la seule façon de
répondre à « qui s'est connecté, quand, depuis où » sans trou dans le journal.

La clé étrangère est conservée : NULL est autorisé, une valeur reste contrainte.
Aucune ligne existante n'est modifiée (toutes ont un org_id non nul).
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "009_audit_org_nullable"
down_revision: Union[str, Sequence[str], None] = "008_unique_ticket_number"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    # SQLite (tests, dev local) construit le schéma depuis les modèles.
    if bind.dialect.name != "postgresql":
        return

    op.alter_column("audit_logs", "org_id", nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Les lignes sans organisation (connexions de super_admin / client OTP)
    # n'ont pas de valeur de remplacement : un downgrade les supprimerait.
    # On refuse plutôt que de détruire des preuves d'audit.
    orphans = bind.execute(
        text("SELECT COUNT(*) FROM audit_logs WHERE org_id IS NULL")
    ).scalar()
    if orphans:
        raise RuntimeError(
            f"{orphans} ligne(s) d'audit sans org_id : downgrade impossible sans "
            "perdre des preuves. Archiver ces lignes avant de revenir en arrière."
        )
    op.alter_column("audit_logs", "org_id", nullable=False)
