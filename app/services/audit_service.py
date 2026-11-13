"""
AuditService — écriture du journal d'audit des actions sensibles.

Utilisé pour les exports de données, les décisions de priorité, les
transferts et les clôtures automatiques. Les lignes sont immuables.
"""
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog

logger = logging.getLogger(__name__)


class AuditService:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def log(
        self,
        *,
        org_id: str,
        action: str,
        actor_id: str | None = None,
        actor_role: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        extra: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> AuditLog:
        """
        Ajoute une ligne au journal d'audit.

        Args:
            org_id: Organisation concernée (isolation multi-tenant)
            action: Une constante de AuditAction
            actor_id: Utilisateur à l'origine de l'action (None = système)
            actor_role: Rôle de l'acteur
            target_type: Nature de l'objet visé (ex. 'ticket', 'branch')
            target_id: Identifiant de l'objet visé
            extra: Détails sérialisables en JSON (filtres d'export, anciennes valeurs…)
            commit: Si False, la ligne est seulement ajoutée à la session (l'appelant commit)

        Returns:
            La ligne AuditLog créée
        """
        entry = AuditLog(
            org_id=org_id,
            actor_id=actor_id,
            actor_role=actor_role,
            action=action,
            target_type=target_type,
            target_id=target_id,
            extra_data=extra,
        )
        self.db.add(entry)
        if commit:
            await self.db.commit()
        return entry
