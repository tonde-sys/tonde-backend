"""
ServiceService — Logique métier pour la gestion des services d'une branch.

Un service = une file dédiée dans une branch.
Exemples : Dépôt, Retrait, Crédit, Consultation médicale, Inscription.

Pattern : Router → Service → Model
Isolation multi-tenant : toutes les requêtes filtrent par org_id.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import HTTPException

from app.models.branch import Branch, Service
from app.models.eta import EtaCounterProfile, EtaServiceProfile
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
        await self._ensure_prefix_available(branch_id, prefix)

        service = Service(
            id=str(uuid.uuid4()),
            org_id=org_id,
            branch_id=branch_id,
            name=data.name,
            description=data.description,
            ticket_prefix=prefix,
            avg_duration_minutes=data.avg_duration_minutes,
            eta_ewma_alpha=data.eta_ewma_alpha,
            eta_min_service_samples=data.eta_min_service_samples,
            eta_min_counter_samples=data.eta_min_counter_samples,
            eta_outlier_min_samples=data.eta_outlier_min_samples,
            eta_outlier_mad_multiplier=data.eta_outlier_mad_multiplier,
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
            HTTPException 409: Réactivation avec un préfixe déjà porté par un autre service actif
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

        previous_avg_minutes = service.avg_duration_minutes
        changes = data.model_dump(exclude_unset=True)
        eta_or_availability_changed = any(
            field in changes
            for field in (
                "avg_duration_minutes",
                "eta_ewma_alpha",
                "eta_min_service_samples",
                "eta_min_counter_samples",
                "eta_outlier_min_samples",
                "eta_outlier_mad_multiplier",
                "is_active",
            )
        )

        # P2-13 : le préfixe n'est pas modifiable (extra="forbid"), mais `is_active`
        # l'est. Réactiver un service dont le préfixe a été repris entre-temps par un
        # autre service actif recréerait deux files produisant les mêmes numéros
        # (C-001 dans deux files le même jour). On refuse donc la réactivation, avec
        # un message qui dit quoi faire, plutôt que de laisser l'ambiguïté s'installer.
        if changes.get("is_active") is True:
            await self._ensure_prefix_available(
                branch_id, service.ticket_prefix, exclude_service_id=service.id
            )

        for field, value in changes.items():
            setattr(service, field, value)

        # L'administrateur garde la main sur la durée de référence : quand il la
        # change, on ré-amorce l'apprentissage (EWMA) avec cette nouvelle valeur
        # sans effacer les observations ni le niveau de confiance déjà acquis.
        if (
            "avg_duration_minutes" in changes
            and changes["avg_duration_minutes"] != previous_avg_minutes
        ):
            seeded_seconds = float(service.avg_duration_minutes * 60)
            now = datetime.now(timezone.utc)
            service_profile = (await self.db.execute(
                select(EtaServiceProfile).where(
                    EtaServiceProfile.service_id == service.id,
                    EtaServiceProfile.org_id == org_id,
                )
            )).scalar_one_or_none()
            if service_profile is not None:
                service_profile.ewma_seconds = seeded_seconds
                service_profile.updated_at = now
            counter_profiles = (await self.db.execute(
                select(EtaCounterProfile).where(
                    EtaCounterProfile.service_id == service.id,
                    EtaCounterProfile.org_id == org_id,
                )
            )).scalars().all()
            for counter_profile in counter_profiles:
                counter_profile.ewma_seconds = seeded_seconds
                counter_profile.updated_at = now

        await self.db.commit()
        await self.db.refresh(service)
        if eta_or_availability_changed:
            # Keep connected queue clients in sync with persisted ETA policy changes.
            # Queue snapshots are best effort; the next authorized ticket read also
            # reconstructs its current forecast from PostgreSQL.
            from app.services.ticket_service import TicketService

            await TicketService(self.db)._publish_queue_snapshot(org_id, branch_id, service_id)
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

        from app.services.ticket_service import TicketService

        await TicketService(self.db)._publish_queue_snapshot(org_id, branch_id, service_id)

    # ── Helpers privés ────────────────────────────────────────────────────────
    async def _ensure_prefix_available(
        self,
        branch_id: str,
        prefix: str,
        *,
        exclude_service_id: str | None = None,
    ) -> None:
        """
        Vérifie qu'aucun AUTRE service actif de la branch ne porte déjà ce préfixe.

        La règle est « au plus un service actif par préfixe et par branch », pas
        « un préfixe par branch » : un service désactivé libère son préfixe, qui
        peut être réutilisé par un nouveau service. C'est la réactivation tardive
        qui est dangereuse, et c'est le seul chemin qui reste après que
        `UpdateServiceRequest` a rendu le préfixe immuable.

        Args:
            branch_id: Branch concernée
            prefix: Préfixe normalisé en majuscules
            exclude_service_id: Service à ignorer (lui-même, en mise à jour)

        Raises:
            HTTPException 409: Un autre service actif porte déjà ce préfixe
        """
        query = select(Service).where(
            Service.branch_id == branch_id,
            Service.ticket_prefix == prefix,
            Service.is_active == True,  # noqa: E712
        )
        if exclude_service_id:
            query = query.where(Service.id != exclude_service_id)

        conflict = (await self.db.execute(query)).scalars().first()
        if conflict is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "PREFIX_TAKEN",
                    "message": (
                        f"Le préfixe '{prefix}' est déjà utilisé par le service actif "
                        f"'{conflict.name}' dans cette branch. Deux services actifs avec "
                        "le même préfixe produiraient les mêmes numéros de ticket."
                    ),
                    "conflicting_service_id": conflict.id,
                    "conflicting_service_name": conflict.name,
                },
            )

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
            "eta_ewma_alpha": service.eta_ewma_alpha,
            "eta_min_service_samples": service.eta_min_service_samples,
            "eta_min_counter_samples": service.eta_min_counter_samples,
            "eta_outlier_min_samples": service.eta_outlier_min_samples,
            "eta_outlier_mad_multiplier": service.eta_outlier_mad_multiplier,
            "is_active": service.is_active,
            "created_at": service.created_at.isoformat() if service.created_at else None,
        }
