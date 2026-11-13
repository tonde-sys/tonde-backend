"""
OrganizationService — Logique métier pour la gestion des organisations.

Pattern : Router → Service → Model
Toutes les opérations DB passent par ce service.
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from fastapi import HTTPException
import secrets
from datetime import datetime, timezone, timedelta

from app.models.organization import Organization
from app.models.user_organization import UserOrganization
from app.schemas.organization import (
    CreateOrganizationRequest,
    UpdateOrganizationRequest,
    OrganizationResponse,
    OrganizationListResponse,
    GenerateInvitationCodeRequest,
    OrganizationMemberResponse,
    UserOrganizationListResponse,
)


class OrganizationService:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(self, data: CreateOrganizationRequest) -> OrganizationResponse:
        """
        Crée une nouvelle organisation.

        Args:
            data: Données de création (name, slug, sector, ...)

        Returns:
            OrganizationResponse avec l'organisation créée

        Raises:
            HTTPException 409: Si le slug est déjà utilisé
        """
        existing = await self.db.execute(
            select(Organization).where(Organization.slug == data.slug)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=409,
                detail={"code": "SLUG_TAKEN", "message": f"Le slug '{data.slug}' est déjà utilisé"}
            )

        org = Organization(**data.model_dump())
        self.db.add(org)
        await self.db.commit()
        await self.db.refresh(org)
        return OrganizationResponse.model_validate(org)

    async def get_by_id(self, org_id: str) -> OrganizationResponse:
        """
        Récupère une organisation par son ID.

        Args:
            org_id: UUID de l'organisation

        Returns:
            OrganizationResponse

        Raises:
            HTTPException 404: Si l'organisation n'existe pas
        """
        result = await self.db.execute(
            select(Organization).where(Organization.id == org_id)
        )
        org = result.scalar_one_or_none()
        if not org:
            raise HTTPException(
                status_code=404,
                detail={"code": "ORG_NOT_FOUND", "message": "Organisation introuvable"}
            )
        return OrganizationResponse.model_validate(org)

    async def list(
        self, page: int = 1, page_size: int = 20, active_only: bool = False
    ) -> OrganizationListResponse:
        """
        Liste toutes les organisations avec pagination.

        Args:
            page: Numéro de page (commence à 1)
            page_size: Nombre d'éléments par page (max 100)
            active_only: Si True, retourne uniquement les organisations actives

        Returns:
            OrganizationListResponse avec items et métadonnées de pagination
        """
        page_size = min(page_size, 100)
        offset = (page - 1) * page_size

        query = select(Organization)
        count_query = select(func.count(Organization.id))

        if active_only:
            query = query.where(Organization.is_active == True)  # noqa: E712
            count_query = count_query.where(Organization.is_active == True)  # noqa: E712

        query = query.order_by(Organization.created_at.desc()).offset(offset).limit(page_size)

        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        result = await self.db.execute(query)
        orgs = result.scalars().all()

        return OrganizationListResponse(
            items=[OrganizationResponse.model_validate(o) for o in orgs],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def update(self, org_id: str, data: UpdateOrganizationRequest) -> OrganizationResponse:
        """
        Met à jour une organisation existante.

        Args:
            org_id: UUID de l'organisation à modifier
            data: Champs à mettre à jour (seuls les champs fournis sont modifiés)

        Returns:
            OrganizationResponse mise à jour

        Raises:
            HTTPException 404: Si l'organisation n'existe pas
        """
        result = await self.db.execute(
            select(Organization).where(Organization.id == org_id)
        )
        org = result.scalar_one_or_none()
        if not org:
            raise HTTPException(
                status_code=404,
                detail={"code": "ORG_NOT_FOUND", "message": "Organisation introuvable"}
            )

        update_data = data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(org, field, value)

        await self.db.commit()
        await self.db.refresh(org)
        return OrganizationResponse.model_validate(org)

    async def delete(self, org_id: str) -> None:
        """
        Supprime une organisation (suppression douce via is_active=False).

        On ne supprime jamais physiquement une organisation en production
        pour préserver l'historique des tickets et utilisateurs associés.

        Args:
            org_id: UUID de l'organisation à désactiver

        Raises:
            HTTPException 404: Si l'organisation n'existe pas
        """
        result = await self.db.execute(
            select(Organization).where(Organization.id == org_id)
        )
        org = result.scalar_one_or_none()
        if not org:
            raise HTTPException(
                status_code=404,
                detail={"code": "ORG_NOT_FOUND", "message": "Organisation introuvable"}
            )

        org.is_active = False
        await self.db.commit()

    # ── S2-08 : Join by Code ──────────────────────────────────────────────────
    async def generate_invitation_code(
        self, org_id: str, data: GenerateInvitationCodeRequest
    ) -> OrganizationResponse:
        """
        Génère un code d'invitation unique pour une organisation.

        Le code est alphanumérique uppercase, 8 caractères.
        Un seul code actif par organisation à la fois.
        L'admin peut régénérer un nouveau code à tout moment — l'ancien est invalidé.

        Args:
            org_id: UUID de l'organisation
            data: Configuration (durée d'expiration)

        Returns:
            OrganizationResponse avec le nouveau code d'invitation

        Raises:
            HTTPException 404: Organisation introuvable
        """
        result = await self.db.execute(
            select(Organization).where(
                Organization.id == org_id,
                Organization.is_active == True,  # noqa: E712
            )
        )
        org = result.scalar_one_or_none()
        if not org:
            raise HTTPException(
                status_code=404,
                detail={"code": "ORG_NOT_FOUND", "message": "Organisation introuvable ou inactive"}
            )

        # Générer un code unique — alphabet restreint pour éviter confusion O/0 I/1
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        while True:
            code = "".join(secrets.choice(alphabet) for _ in range(8))
            # Vérifier l'unicité en DB
            existing = await self.db.execute(
                select(Organization).where(Organization.invitation_code == code)
            )
            if not existing.scalar_one_or_none():
                break

        org.invitation_code = code
        org.invitation_expires_at = datetime.now(timezone.utc) + timedelta(days=data.expires_in_days)
        org.invitation_code_active = True

        await self.db.commit()
        await self.db.refresh(org)
        return OrganizationResponse.model_validate(org)

    async def join_by_code(
        self, code: str, user_id: str, member_number: str | None = None
    ) -> OrganizationMemberResponse:
        """
        Permet à un utilisateur de rejoindre une organisation via un code d'invitation.

        Validations de sécurité :
        - Code existant et actif
        - Code non expiré
        - Organisation active
        - Utilisateur non déjà membre (contrainte uq_user_organization)

        Args:
            code: Code d'invitation en uppercase
            user_id: ID de l'utilisateur qui rejoint
            member_number: Numéro optionnel (compte bancaire, dossier...)

        Returns:
            OrganizationMemberResponse avec les détails de l'appartenance

        Raises:
            HTTPException 404: Code invalide ou expiré
            HTTPException 409: Utilisateur déjà membre
        """
        code = code.strip().upper()

        # Vérifier le code — on ne distingue PAS "code inexistant" de "code expiré"
        # pour éviter l'énumération de codes valides
        result = await self.db.execute(
            select(Organization).where(
                Organization.invitation_code == code,
                Organization.invitation_code_active == True,  # noqa: E712
                Organization.is_active == True,  # noqa: E712
            )
        )
        org = result.scalar_one_or_none()
        if not org:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "INVALID_INVITATION_CODE",
                    "message": "Code d'invitation invalide ou expiré. Vérifiez le code et réessayez."
                }
            )

        # Vérifier l'expiration séparément (après avoir confirmé l'existence)
        now = datetime.now(timezone.utc)
        expires = org.invitation_expires_at
        if expires:
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if now > expires:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "code": "INVALID_INVITATION_CODE",
                        "message": "Code d'invitation invalide ou expiré. Vérifiez le code et réessayez."
                    }
                )

        # Vérifier que l'utilisateur n'est pas déjà membre
        existing_membership = await self.db.execute(
            select(UserOrganization).where(
                UserOrganization.user_id == user_id,
                UserOrganization.organization_id == org.id,
            )
        )
        existing = existing_membership.scalar_one_or_none()
        if existing:
            if existing.status == "active":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "ALREADY_MEMBER",
                        "message": "Vous êtes déjà membre de cette organisation."
                    }
                )
            else:
                # Réactiver une appartenance inactive
                existing.status = "active"
                existing.member_number = member_number
                await self.db.commit()
                await self.db.refresh(existing)
                return OrganizationMemberResponse(
                    id=existing.id,
                    user_id=existing.user_id,
                    organization_id=existing.organization_id,
                    organization_name=org.name,
                    member_number=existing.member_number,
                    status=existing.status,
                    joined_at=existing.created_at,
                )

        # Créer l'appartenance
        membership = UserOrganization(
            user_id=user_id,
            organization_id=org.id,
            member_number=member_number,
            status="active",
        )
        self.db.add(membership)
        await self.db.commit()
        await self.db.refresh(membership)

        return OrganizationMemberResponse(
            id=membership.id,
            user_id=membership.user_id,
            organization_id=membership.organization_id,
            organization_name=org.name,
            member_number=membership.member_number,
            status=membership.status,
            joined_at=membership.created_at,
        )

    async def get_user_organizations(self, user_id: str) -> UserOrganizationListResponse:
        """
        Liste toutes les organisations actives d'un utilisateur.

        Args:
            user_id: ID de l'utilisateur

        Returns:
            UserOrganizationListResponse avec les organisations rejointes
        """
        result = await self.db.execute(
            select(UserOrganization, Organization)
            .join(Organization, UserOrganization.organization_id == Organization.id)
            .where(
                UserOrganization.user_id == user_id,
                UserOrganization.status == "active",
                Organization.is_active == True,  # noqa: E712
            )
            .order_by(UserOrganization.created_at.desc())
        )
        rows = result.all()

        items = [
            OrganizationMemberResponse(
                id=membership.id,
                user_id=membership.user_id,
                organization_id=membership.organization_id,
                organization_name=org.name,
                member_number=membership.member_number,
                status=membership.status,
                joined_at=membership.created_at,
            )
            for membership, org in rows
        ]

        return UserOrganizationListResponse(items=items, total=len(items))

    async def leave_organization(self, user_id: str, org_id: str) -> None:
        """
        Permet à un utilisateur de quitter une organisation (soft delete).

        La ligne UserOrganization est marquée inactive — jamais supprimée
        pour conserver l'historique.

        Args:
            user_id: ID de l'utilisateur qui quitte
            org_id: ID de l'organisation à quitter

        Raises:
            HTTPException 404: Appartenance introuvable
        """
        result = await self.db.execute(
            select(UserOrganization).where(
                UserOrganization.user_id == user_id,
                UserOrganization.organization_id == org_id,
                UserOrganization.status == "active",
            )
        )
        membership = result.scalar_one_or_none()
        if not membership:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "MEMBERSHIP_NOT_FOUND",
                    "message": "Vous n'êtes pas membre de cette organisation."
                }
            )

        membership.status = "inactive"
        await self.db.commit()
