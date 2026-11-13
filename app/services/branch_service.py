"""
BranchService — Logique métier pour la gestion des branches (ex-agences).

Sprint 2 — S2-01 : renommage Agency → Branch.

Règle de sécurité absolue : toute opération filtre par org_id.
Un admin ne peut voir et modifier que les branches de son organisation.

Pattern : Router → Service → Model
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from fastapi import HTTPException

from app.models.branch import Branch
from app.models.organization import Organization
from app.schemas.branch import (
    CreateBranchRequest,
    UpdateBranchRequest,
    BranchResponse,
    BranchListResponse,
)
from app.schemas.branch_config import BranchConfigResponse, UpdateBranchConfigRequest


class BranchService:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _verify_org_exists(self, org_id: str) -> None:
        """Vérifie que l'organisation existe et est active."""
        result = await self.db.execute(
            select(Organization).where(
                Organization.id == org_id,
                Organization.is_active == True,  # noqa: E712
            )
        )
        if not result.scalar_one_or_none():
            raise HTTPException(
                status_code=404,
                detail={"code": "ORG_NOT_FOUND", "message": "Organisation introuvable ou inactive"},
            )

    async def create(
        self, org_id: str, data: CreateBranchRequest, caller_org_id: str | None
    ) -> BranchResponse:
        """
        Crée une nouvelle branch rattachée à une organisation.

        Args:
            org_id: ID de l'organisation cible
            data: Données de création de la branch
            caller_org_id: org_id de l'utilisateur appelant (None = super_admin)

        Returns:
            BranchResponse avec la branch créée

        Raises:
            HTTPException 403: Si l'appelant tente de créer dans une org qui n'est pas la sienne
            HTTPException 404: Si l'organisation n'existe pas
            HTTPException 409: Si le slug est déjà utilisé
        """
        # Isolation multi-tenant : un admin_org ne peut créer que dans son org
        if caller_org_id is not None and caller_org_id != org_id:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "FORBIDDEN",
                    "message": "Vous ne pouvez pas créer une branch dans une autre organisation",
                },
            )

        await self._verify_org_exists(org_id)

        existing = await self.db.execute(
            select(Branch).where(Branch.slug == data.slug)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=409,
                detail={"code": "SLUG_TAKEN", "message": f"Le slug '{data.slug}' est déjà utilisé"},
            )

        branch = Branch(org_id=org_id, **data.model_dump())
        self.db.add(branch)
        await self.db.commit()
        await self.db.refresh(branch)
        return BranchResponse.model_validate(branch)

    async def get_by_id(self, branch_id: str, caller_org_id: str | None) -> BranchResponse:
        """
        Récupère une branch par son ID.

        Args:
            branch_id: UUID de la branch
            caller_org_id: org_id de l'utilisateur (None = super_admin, voit tout)

        Returns:
            BranchResponse

        Raises:
            HTTPException 404: Si la branch n'existe pas ou n'appartient pas à l'org de l'appelant
        """
        query = select(Branch).where(Branch.id == branch_id)

        # Isolation multi-tenant : filtrer par org_id sauf pour super_admin
        if caller_org_id is not None:
            query = query.where(Branch.org_id == caller_org_id)

        result = await self.db.execute(query)
        branch = result.scalar_one_or_none()
        if not branch:
            raise HTTPException(
                status_code=404,
                detail={"code": "BRANCH_NOT_FOUND", "message": "Branch introuvable"},
            )
        return BranchResponse.model_validate(branch)

    async def list(
        self,
        caller_org_id: str | None,
        org_id: str | None = None,
        page: int = 1,
        page_size: int = 20,
        active_only: bool = False,
    ) -> BranchListResponse:
        """
        Liste les branches avec pagination.

        Args:
            caller_org_id: org_id de l'appelant — force le filtre multi-tenant
            org_id: filtre optionnel par org (super_admin seulement)
            page: numéro de page
            page_size: éléments par page (max 100)
            active_only: si True, retourne uniquement les branches actives

        Returns:
            BranchListResponse avec items et métadonnées de pagination
        """
        page_size = min(page_size, 100)
        offset = (page - 1) * page_size

        query = select(Branch)
        count_query = select(func.count(Branch.id))

        # Isolation multi-tenant stricte
        effective_org_id = caller_org_id if caller_org_id is not None else org_id
        if effective_org_id:
            query = query.where(Branch.org_id == effective_org_id)
            count_query = count_query.where(Branch.org_id == effective_org_id)

        if active_only:
            query = query.where(Branch.is_active == True)  # noqa: E712
            count_query = count_query.where(Branch.is_active == True)  # noqa: E712

        query = query.order_by(Branch.created_at.desc()).offset(offset).limit(page_size)

        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        result = await self.db.execute(query)
        branches = result.scalars().all()

        return BranchListResponse(
            items=[BranchResponse.model_validate(b) for b in branches],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def update(
        self, branch_id: str, data: UpdateBranchRequest, caller_org_id: str | None
    ) -> BranchResponse:
        """
        Met à jour une branch existante.

        Args:
            branch_id: UUID de la branch à modifier
            data: Champs à mettre à jour (seuls les champs fournis sont modifiés)
            caller_org_id: org_id de l'appelant pour vérification d'appartenance

        Returns:
            BranchResponse mise à jour

        Raises:
            HTTPException 404: Si la branch n'existe pas ou n'appartient pas à l'org
        """
        query = select(Branch).where(Branch.id == branch_id)
        if caller_org_id is not None:
            query = query.where(Branch.org_id == caller_org_id)

        result = await self.db.execute(query)
        branch = result.scalar_one_or_none()
        if not branch:
            raise HTTPException(
                status_code=404,
                detail={"code": "BRANCH_NOT_FOUND", "message": "Branch introuvable"},
            )

        update_data = data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(branch, field, value)

        await self.db.commit()
        await self.db.refresh(branch)
        return BranchResponse.model_validate(branch)

    async def delete(self, branch_id: str, caller_org_id: str | None) -> None:
        """
        Désactive une branch (soft delete).

        Args:
            branch_id: UUID de la branch à désactiver
            caller_org_id: org_id de l'appelant pour vérification d'appartenance

        Raises:
            HTTPException 404: Si la branch n'existe pas ou n'appartient pas à l'org
        """
        query = select(Branch).where(Branch.id == branch_id)
        if caller_org_id is not None:
            query = query.where(Branch.org_id == caller_org_id)

        result = await self.db.execute(query)
        branch = result.scalar_one_or_none()
        if not branch:
            raise HTTPException(
                status_code=404,
                detail={"code": "BRANCH_NOT_FOUND", "message": "Branch introuvable"},
            )

        branch.is_active = False
        await self.db.commit()

    # ── Configuration file d'attente (S2-10) ─────────────────────────────────

    async def get_config(
        self, branch_id: str, caller_org_id: str | None
    ) -> "BranchConfigResponse":
        """
        Retourne la configuration de la file d'attente d'une branch.

        Args:
            branch_id: UUID de la branch
            caller_org_id: org_id de l'appelant (None = super_admin)

        Returns:
            BranchConfigResponse avec tous les champs de config

        Raises:
            HTTPException 404: Si la branch est introuvable ou hors org
        """
        query = select(Branch).where(Branch.id == branch_id)
        if caller_org_id is not None:
            query = query.where(Branch.org_id == caller_org_id)

        result = await self.db.execute(query)
        branch = result.scalar_one_or_none()
        if not branch:
            raise HTTPException(
                status_code=404,
                detail={"code": "BRANCH_NOT_FOUND", "message": "Branch introuvable"},
            )

        return BranchConfigResponse(
            branch_id=branch.id,
            max_daily_tickets=branch.max_daily_tickets,
            avg_service_minutes=branch.avg_service_minutes,
            max_wait_minutes_alert=branch.max_wait_minutes_alert,
            opens_at=branch.opens_at,
            closes_at=branch.closes_at,
            operating_hours=branch.operating_hours,
            enable_sms_reminders=branch.enable_sms_reminders,
            reminder_interval_minutes=branch.reminder_interval_minutes,
            supported_languages=branch.supported_languages or ["fr"],
        )

    async def update_config(
        self, branch_id: str, data: "UpdateBranchConfigRequest", caller_org_id: str | None
    ) -> "BranchConfigResponse":
        """
        Met à jour la configuration de la file d'attente (PATCH partiel).

        Seuls les champs fournis dans data sont modifiés.
        Les champs absents conservent leur valeur actuelle.

        Args:
            branch_id: UUID de la branch à configurer
            data: Champs de config à mettre à jour
            caller_org_id: org_id de l'appelant pour vérification d'appartenance

        Returns:
            BranchConfigResponse mise à jour

        Raises:
            HTTPException 404: Si la branch est introuvable ou hors org
        """
        query = select(Branch).where(Branch.id == branch_id)
        if caller_org_id is not None:
            query = query.where(Branch.org_id == caller_org_id)

        result = await self.db.execute(query)
        branch = result.scalar_one_or_none()
        if not branch:
            raise HTTPException(
                status_code=404,
                detail={"code": "BRANCH_NOT_FOUND", "message": "Branch introuvable"},
            )

        # PATCH partiel — ne modifier que les champs fournis
        update_data = data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(branch, field, value)

        await self.db.commit()
        await self.db.refresh(branch)

        # Retourner la config complète mise à jour
        return await self.get_config(branch_id, caller_org_id)
