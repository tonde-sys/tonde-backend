"""un seul service actif par préfixe et par branch

Revision ID: 010_unique_active_service_prefix
Revises: 009_audit_org_nullable
Create Date: 2026-11-16

Constat P2-13 de l'audit. La vérification applicative de `ServiceService.create`
ne regardait que les services ACTIFS. Désactiver « Caisse » (préfixe C), créer
« Crédit » (C), puis réactiver « Caisse » produisait deux services actifs avec le
même préfixe — donc deux files qui émettent des `C-001` différents le même jour,
et un écran de salle qui affiche « C-002 au guichet 3 » sans dire lequel.

Le correctif applicatif est dans `ServiceService._ensure_prefix_available`. Cet
index est la ceinture : il rend l'invariant vrai même si un futur chemin de code
oublie de l'appeler.

Index PARTIEL (`WHERE is_active`) et non unique global : un service désactivé
libère son préfixe, qui doit rester réutilisable par un nouveau service. Seuls
les services actifs se disputent la lettre.

Même précaution que la migration 008 : une base existante peut déjà contenir des
doublons (c'est précisément le bug). Créer l'index échouerait alors et ferait
échouer TOUTE la migration, donc le déploiement. On vérifie d'abord, on avertit,
et on n'échoue pas : le correctif applicatif protège les nouveaux cas.
"""
import logging
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

logger = logging.getLogger("alembic.runtime.migration")

# revision identifiers, used by Alembic.
revision: str = "010_unique_active_service_prefix"
down_revision: Union[str, Sequence[str], None] = "009_audit_org_nullable"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # Index partiel : syntaxe PostgreSQL. SQLite (tests, dev local) construit le
    # schéma depuis les modèles et n'exécute pas les migrations.
    if bind.dialect.name != "postgresql":
        return

    duplicates = bind.execute(
        text(
            """
            SELECT count(*) FROM (
                SELECT 1
                FROM services
                WHERE is_active
                GROUP BY branch_id, ticket_prefix
                HAVING count(*) > 1
            ) AS d
            """
        )
    ).scalar()

    if duplicates:
        logger.warning(
            "[010] %s préfixe(s) porté(s) par plusieurs services actifs : index unique "
            "NON créé. Le correctif applicatif bloque les nouveaux cas, mais des files "
            "ambiguës subsistent. Identifier les branches concernées avec :\n"
            "  SELECT branch_id, ticket_prefix, count(*) FROM services WHERE is_active\n"
            "  GROUP BY 1, 2 HAVING count(*) > 1;\n"
            "puis désactiver le service en trop et rejouer la migration.",
            duplicates,
        )
        return

    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_services_active_prefix
        ON services (branch_id, ticket_prefix)
        WHERE is_active
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("DROP INDEX IF EXISTS uq_services_active_prefix")
