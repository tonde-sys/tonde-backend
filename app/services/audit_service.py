"""
AuditService — écriture du journal d'audit des actions sensibles.

Utilisé pour les exports de données, les décisions de priorité, les
transferts et les clôtures automatiques. Les lignes sont immuables.
"""
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog

logger = logging.getLogger(__name__)


class AuditService:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def log(
        self,
        *,
        org_id: str | None,
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
            org_id: Organisation concernée (isolation multi-tenant). `None` pour
                un acteur sans organisation — super_admin, client OTP — ou pour
                une action système transverse. C'est le cas d'usage ouvert par
                la migration 009 (constat P3-17).
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

    async def search(
        self,
        *,
        org_id: str | None,
        action: str | None = None,
        actor_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[AuditLog], int]:
        """
        Lit le journal d'audit, du plus récent au plus ancien.

        Args:
            org_id: Organisation à consulter. `None` = toutes (réservé au
                super-admin, qui n'est rattaché à aucune organisation).
            action: Filtre sur une constante de AuditAction
            actor_id: Filtre sur l'auteur
            date_from / date_to: Bornes de période (incluses)
            page: Page à partir de 1
            page_size: Taille de page (l'appelant plafonne)

        Returns:
            (lignes, total) — le total sert à la pagination côté interface.
        """
        conditions = []
        if org_id is not None:
            conditions.append(AuditLog.org_id == org_id)
        if action:
            conditions.append(AuditLog.action == action)
        if actor_id:
            conditions.append(AuditLog.actor_id == actor_id)
        if date_from is not None:
            conditions.append(AuditLog.created_at >= date_from)
        if date_to is not None:
            conditions.append(AuditLog.created_at <= date_to)

        total = (
            await self.db.execute(select(func.count(AuditLog.id)).where(*conditions))
        ).scalar() or 0

        rows = (
            await self.db.execute(
                select(AuditLog)
                .where(*conditions)
                .order_by(AuditLog.created_at.desc())
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
        ).scalars().all()

        return list(rows), total
