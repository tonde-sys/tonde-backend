"""
CounterService — Logique métier pour la gestion des guichets.

Règle de sécurité absolue : toute opération filtre par org_id.
Un admin ne peut voir et modifier que les guichets de son organisation.
Quand l'agence du chemin est fournie, elle est VÉRIFIÉE : un guichet d'une autre
agence ne se laisse pas atteindre en changeant l'URL.

Ouvrir / fermer un guichet prévient en temps réel les clients de la salle d'attente.

Pattern : Router → Service → Model
"""
import logging

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.branch import Branch
from app.models.counter import Counter
from app.schemas.counter import (
    CounterListResponse,
    CounterResponse,
    CreateCounterRequest,
    UpdateCounterRequest,
)
from app.websocket.events import EventType
from app.websocket.publisher import publish_event

logger = logging.getLogger(__name__)


class CounterService:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _verify_agency_belongs_to_org(
        self, agency_id: str, org_id: str
    ) -> Branch:
        """
        Vérifie que la branch existe et appartient à l'organisation.

        Raises:
            HTTPException 404: Si la branch n'existe pas ou n'appartient pas à l'org
        """
        result = await self.db.execute(
            select(Branch).where(
                Branch.id == agency_id,
                Branch.org_id == org_id,
                Branch.is_active == True,  # noqa: E712
            )
        )
        branch = result.scalar_one_or_none()
        if not branch:
            raise HTTPException(
                status_code=404,
                detail={"code": "AGENCY_NOT_FOUND", "message": "Agence introuvable ou inactive"}
            )
        return branch

    async def _get_counter(
        self,
        counter_id: str,
        org_id: str,
        agency_id: str | None = None,
        *,
        active_only: bool = False,
    ) -> Counter:
        """
        Récupère un guichet de l'organisation (et de l'agence si fournie).

        Raises:
            HTTPException 404: Guichet introuvable dans ce périmètre
        """
        query = select(Counter).where(Counter.id == counter_id, Counter.org_id == org_id)
        if agency_id is not None:
            query = query.where(Counter.agency_id == agency_id)
        if active_only:
            query = query.where(Counter.is_active == True)  # noqa: E712
        counter = (await self.db.execute(query)).scalar_one_or_none()
        if not counter:
            raise HTTPException(
                status_code=404,
                detail={"code": "COUNTER_NOT_FOUND", "message": "Guichet introuvable"}
            )
        return counter

    async def create(
        self, org_id: str, agency_id: str, data: CreateCounterRequest
    ) -> CounterResponse:
        """
        Crée un nouveau guichet dans une agence.

        Args:
            org_id: ID de l'organisation (isolation multi-tenant)
            agency_id: ID de l'agence où créer le guichet
            data: Données de création (name, description)

        Returns:
            CounterResponse avec le guichet créé

        Raises:
            HTTPException 404: Si l'agence n'existe pas dans l'org
            HTTPException 409: Si un guichet avec ce nom existe déjà dans l'agence
        """
        await self._verify_agency_belongs_to_org(agency_id, org_id)

        # Vérifier unicité du nom dans l'agence
        existing = await self.db.execute(
            select(Counter).where(
                Counter.agency_id == agency_id,
                Counter.name == data.name.strip(),
                Counter.is_active == True,  # noqa: E712
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "COUNTER_NAME_TAKEN",
                    "message": f"Un guichet nommé '{data.name}' existe déjà dans cette agence",
                }
            )

        counter = Counter(
            org_id=org_id,
            agency_id=agency_id,
            **data.model_dump(),
        )
        self.db.add(counter)
        await self.db.commit()
        await self.db.refresh(counter)
        return CounterResponse.model_validate(counter)

    async def get_by_id(
        self, counter_id: str, org_id: str, agency_id: str | None = None
    ) -> CounterResponse:
        """
        Récupère un guichet par son ID.

        Args:
            counter_id: UUID du guichet
            org_id: org_id de l'appelant (isolation multi-tenant)
            agency_id: Si fourni, le guichet doit appartenir à cette agence

        Returns:
            CounterResponse

        Raises:
            HTTPException 404: Si le guichet n'existe pas dans l'org / l'agence
        """
        counter = await self._get_counter(counter_id, org_id, agency_id)
        return CounterResponse.model_validate(counter)

    async def list(
        self,
        org_id: str,
        agency_id: str,
        page: int = 1,
        page_size: int = 20,
        active_only: bool = False,
    ) -> CounterListResponse:
        """
        Liste les guichets d'une agence avec pagination.

        Args:
            org_id: Isolation multi-tenant obligatoire
            agency_id: Filtrer par agence
            page: Numéro de page
            page_size: Éléments par page (max 100)
            active_only: Si True, retourne uniquement les guichets actifs

        Returns:
            CounterListResponse avec items et métadonnées de pagination
        """
        await self._verify_agency_belongs_to_org(agency_id, org_id)

        page_size = min(page_size, 100)
        offset = (page - 1) * page_size

        query = select(Counter).where(
            Counter.org_id == org_id,
            Counter.agency_id == agency_id,
        )
        count_query = select(func.count(Counter.id)).where(
            Counter.org_id == org_id,
            Counter.agency_id == agency_id,
        )

        if active_only:
            query = query.where(Counter.is_active == True)  # noqa: E712
            count_query = count_query.where(Counter.is_active == True)  # noqa: E712

        query = query.order_by(Counter.created_at.asc()).offset(offset).limit(page_size)

        total = (await self.db.execute(count_query)).scalar_one()
        counters = (await self.db.execute(query)).scalars().all()

        return CounterListResponse(
            items=[CounterResponse.model_validate(c) for c in counters],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def update(
        self,
        counter_id: str,
        org_id: str,
        data: UpdateCounterRequest,
        agency_id: str | None = None,
    ) -> CounterResponse:
        """
        Met à jour un guichet existant.

        Args:
            counter_id: UUID du guichet à modifier
            org_id: org_id de l'appelant (isolation multi-tenant)
            data: Champs à mettre à jour
            agency_id: Si fourni, le guichet doit appartenir à cette agence

        Returns:
            CounterResponse mis à jour

        Raises:
            HTTPException 404: Si le guichet n'existe pas dans l'org / l'agence
        """
        counter = await self._get_counter(counter_id, org_id, agency_id)

        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(counter, field, value)

        await self.db.commit()
        await self.db.refresh(counter)
        return CounterResponse.model_validate(counter)

    async def open(
        self, counter_id: str, org_id: str, agency_id: str | None = None
    ) -> CounterResponse:
        """
        Ouvre un guichet pour recevoir des clients.

        Transition : is_open = False → True. Prévient la salle d'attente (COUNTER_OPEN).

        Args:
            counter_id: UUID du guichet
            org_id: Isolation multi-tenant
            agency_id: Si fourni, le guichet doit appartenir à cette agence

        Returns:
            CounterResponse mis à jour

        Raises:
            HTTPException 400: Si le guichet est déjà ouvert
            HTTPException 404: Si le guichet n'existe pas
        """
        counter = await self._get_counter(counter_id, org_id, agency_id, active_only=True)
        if counter.is_open:
            raise HTTPException(
                status_code=400,
                detail={"code": "COUNTER_ALREADY_OPEN", "message": "Ce guichet est déjà ouvert"}
            )

        counter.is_open = True
        await self.db.commit()
        await self.db.refresh(counter)
        await self._announce(counter, EventType.COUNTER_OPEN)
        return CounterResponse.model_validate(counter)

    async def close(
        self, counter_id: str, org_id: str, agency_id: str | None = None
    ) -> CounterResponse:
        """
        Ferme un guichet — plus de tickets appelés depuis ce guichet.

        Transition : is_open = True → False. Prévient la salle d'attente (COUNTER_CLOSE).
        Un ticket en cours (appelé ou en service) n'est PAS perdu : il reste rattaché à
        ce guichet, son agent peut le terminer ; seul `current_ticket_id` est libéré.

        Args:
            counter_id: UUID du guichet
            org_id: Isolation multi-tenant
            agency_id: Si fourni, le guichet doit appartenir à cette agence

        Returns:
            CounterResponse mis à jour

        Raises:
            HTTPException 400: Si le guichet est déjà fermé
            HTTPException 404: Si le guichet n'existe pas
        """
        counter = await self._get_counter(counter_id, org_id, agency_id)
        if not counter.is_open:
            raise HTTPException(
                status_code=400,
                detail={"code": "COUNTER_ALREADY_CLOSED", "message": "Ce guichet est déjà fermé"}
            )

        counter.is_open = False
        counter.current_ticket_id = None
        await self.db.commit()
        await self.db.refresh(counter)
        await self._announce(counter, EventType.COUNTER_CLOSE)
        return CounterResponse.model_validate(counter)

    async def delete(
        self, counter_id: str, org_id: str, agency_id: str | None = None
    ) -> None:
        """
        Désactive un guichet (soft delete).

        Args:
            counter_id: UUID du guichet
            org_id: Isolation multi-tenant
            agency_id: Si fourni, le guichet doit appartenir à cette agence

        Raises:
            HTTPException 404: Si le guichet n'existe pas dans l'org / l'agence
        """
        counter = await self._get_counter(counter_id, org_id, agency_id)

        was_open = counter.is_open
        counter.is_open = False
        counter.is_active = False
        await self.db.commit()
        if was_open:
            await self._announce(counter, EventType.COUNTER_CLOSE)

    async def _announce(self, counter: Counter, event_type: EventType) -> None:
        """Diffuse l'ouverture/fermeture d'un guichet à la salle d'attente. Ne lève jamais."""
        await publish_event(counter.org_id, {
            "type": event_type.value,
            "agency_id": counter.agency_id,
            "counter_id": counter.id,
            "counter_name": counter.name,
        })
