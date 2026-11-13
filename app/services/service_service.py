"""
ServiceService — Logique métier pour la gestion des services d'une branch.

Un service = une file dédiée dans une branch.
Exemples : Dépôt, Retrait, Crédit, Consultation médicale, Inscription.

Pattern : Router → Service → Model
Isolation multi-tenant : toutes les requêtes filtrent par org_id.
"""
import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import HTTPException

from app.models.branch import Branch, Service
from app.schemas.service import CreateServiceRequest, UpdateServiceRequest


class ServiceService:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(self, org_id: str, branch_id: str, data: CreateServiceRequest) -> dict:
        """
        Crée un nouveau service dans une branch.

        Args:
            org_id: ID de l'organisation (isolation multi-tenant)
            branch_id: ID de la branch parente
            data: Données validées (name, description, ticket_prefix, avg_duration_minutes, is_active)

        Returns:
            Dict du service créé

        Raises:
            HTTPException 404: Branch introuvable ou hors org
            HTTPException 409: Préfixe de ticket déjà utilisé dans cette branch
        """
        # Vérifier que la branch existe et appartient à l'org
        await self._verify_branch(branch_id, org_id)

        # Vérifier l'unicité du préfixe dans la branch (déjà normalisé en majuscules)
        prefix = data.ticket_prefix
        existing = await self.db.execute(
            select(Service).where(
                Service.branch_id == branch_id,
                Service.ticket_prefix == prefix,
                Service.is_active == True,  # noqa: E712
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "PREFIX_TAKEN",
                    "message": f"Le préfixe '{prefix}' est déjà utilisé par un service actif dans cette branch",
                },
            )

        service = Service(
            id=str(uuid.uuid4()),
            org_id=org_id,
            branch_id=branch_id,
            name=data.name,
            description=data.description,
            ticket_prefix=prefix,
            avg_duration_minutes=data.avg_duration_minutes,
            is_active=data.is_active,
        )
        self.db.add(service)
        await self.db.commit()
        await self.db.refresh(service)
        return self._to_dict(service)

    async def get_by_id(self, service_id: str, branch_id: str, org_id: str | None) -> dict:
        """
        Récupère un service par son ID.

        Args:
            service_id: UUID du service
            branch_id: UUID de la branch parente
            org_id: org_id de l'appelant (None = public/super_admin)

        Returns:
            Dict du service

        Raises:
            HTTPException 404: Service introuvable
        """
        query = select(Service).where(
            Service.id == service_id,
            Service.branch_id == branch_id,
        )
        if org_id:
            query = query.where(Service.org_id == org_id)

        result = await self.db.execute(query)
        service = result.scalar_one_or_none()
        if not service:
            raise HTTPException(
                status_code=404,
                detail={"code": "SERVICE_NOT_FOUND", "message": "Service introuvable"},
            )
        return self._to_dict(service)

    async def list(
        self,
        branch_id: str,
        org_id: str | None,
        active_only: bool = False,
    ) -> list[dict]:
        """
        Liste les services d'une branch.

        Args:
            branch_id: UUID de la branch
            org_id: org_id de l'appelant (None = public, voit les actifs seulement)
            active_only: Si True, retourne seulement les services actifs

        Returns:
            Liste de dicts des services
        """
        query = select(Service).where(Service.branch_id == branch_id)

        if org_id:
            query = query.where(Service.org_id == org_id)

        if active_only:
            query = query.where(Service.is_active == True)  # noqa: E712

        query = query.order_by(Service.ticket_prefix)

        result = await self.db.execute(query)
        services = result.scalars().all()
        return [self._to_dict(s) for s in services]

    async def update(
        self, service_id: str, branch_id: str, org_id: str, data: UpdateServiceRequest
    ) -> dict:
        """
        Met à jour un service (PATCH partiel : seuls les champs fournis changent).

        Args:
            service_id: UUID du service à modifier
            branch_id: UUID de la branch parente
            org_id: org_id de l'appelant pour vérification
            data: Champs validés à mettre à jour

        Returns:
            Dict du service mis à jour

        Raises:
            HTTPException 404: Service introuvable
        """
        result = await self.db.execute(
            select(Service).where(
                Service.id == service_id,
                Service.branch_id == branch_id,
                Service.org_id == org_id,
            )
        )
        service = result.scalar_one_or_none()
        if not service:
            raise HTTPException(
                status_code=404,
                detail={"code": "SERVICE_NOT_FOUND", "message": "Service introuvable"},
            )

        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(service, field, value)

        await self.db.commit()
        await self.db.refresh(service)
        return self._to_dict(service)

    async def delete(self, service_id: str, branch_id: str, org_id: str) -> None:
        """
        Désactive un service (soft delete).

        Args:
            service_id: UUID du service à désactiver
            branch_id: UUID de la branch parente
            org_id: org_id de l'appelant pour vérification

        Raises:
            HTTPException 404: Service introuvable
        """
        result = await self.db.execute(
            select(Service).where(
                Service.id == service_id,
                Service.branch_id == branch_id,
                Service.org_id == org_id,
            )
        )
        service = result.scalar_one_or_none()
        if not service:
            raise HTTPException(
                status_code=404,
                detail={"code": "SERVICE_NOT_FOUND", "message": "Service introuvable"},
            )

        service.is_active = False
        await self.db.commit()

    # ── Helpers privés ────────────────────────────────────────────────────────
    async def _verify_branch(self, branch_id: str, org_id: str) -> None:
        """Vérifie que la branch existe et appartient à l'org."""
        result = await self.db.execute(
            select(Branch).where(
                Branch.id == branch_id,
                Branch.org_id == org_id,
                Branch.is_active == True,  # noqa: E712
            )
        )
        if not result.scalar_one_or_none():
            raise HTTPException(
                status_code=404,
                detail={"code": "BRANCH_NOT_FOUND", "message": "Branch introuvable ou inactive"},
            )

    @staticmethod
    def _to_dict(service: Service) -> dict:
        """Sérialise un Service en dict — jamais retourner l'objet ORM brut."""
        return {
            "id": service.id,
            "org_id": service.org_id,
            "branch_id": service.branch_id,
            "name": service.name,
            "description": service.description,
            "ticket_prefix": service.ticket_prefix,
            "avg_duration_minutes": service.avg_duration_minutes,
            "is_active": service.is_active,
            "created_at": service.created_at.isoformat() if service.created_at else None,
        }
