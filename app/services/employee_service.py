"""
EmployeeService — Logique métier pour la gestion des employés.

Un Employee est le lien entre un User et une Organisation.
Il définit le rôle opérationnel de l'utilisateur (agent, superviseur, admin).

Règle de sécurité absolue : toute opération filtre par org_id.

Pattern : Router → Service → Model
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from fastapi import HTTPException

from app.models.branch import Branch
from app.models.counter import Counter
from app.models.employee import Employee, EmployeeStatus
from app.models.user import User, UserRole
from app.schemas.employee import (
    CreateEmployeeRequest,
    UpdateEmployeeRequest,
    EmployeeResponse,
    EmployeeListResponse,
)


class EmployeeService:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _verify_user_exists(self, user_id: str) -> User:
        """
        Vérifie que l'utilisateur existe et est actif.

        Raises:
            HTTPException 404: Si l'utilisateur n'existe pas
        """
        result = await self.db.execute(
            select(User).where(
                User.id == user_id,
                User.is_active == True,  # noqa: E712
            )
        )
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(
                status_code=404,
                detail={"code": "USER_NOT_FOUND", "message": "Utilisateur introuvable ou inactif"}
            )
        return user

    async def _validate_assignment(
        self, org_id: str, agency_id: str | None, counter_id: str | None
    ) -> tuple[str | None, str | None]:
        """
        Vérifie qu'une affectation (agence, guichet) appartient à l'organisation.

        - L'agence doit exister dans l'organisation (sinon 404).
        - Le guichet doit exister dans l'organisation (sinon 404) ET appartenir à
          l'agence indiquée (sinon 400). Si seul le guichet est fourni, l'agence en est déduite.

        Sans ce contrôle, un admin pouvait affecter un employé au guichet d'une
        autre organisation en devinant son identifiant.

        Returns:
            (agency_id, counter_id) normalisés (l'agence est déduite du guichet si absente)

        Raises:
            HTTPException 400: Guichet d'une autre agence que celle indiquée
            HTTPException 404: Agence ou guichet introuvable dans l'organisation
        """
        if agency_id:
            branch = (await self.db.execute(
                select(Branch.id).where(Branch.id == agency_id, Branch.org_id == org_id)
            )).scalar_one_or_none()
            if branch is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "BRANCH_NOT_FOUND", "message": "Agence introuvable dans cette organisation"},
                )

        if counter_id:
            counter_branch = (await self.db.execute(
                select(Counter.agency_id).where(Counter.id == counter_id, Counter.org_id == org_id)
            )).scalar_one_or_none()
            if counter_branch is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "COUNTER_NOT_FOUND", "message": "Guichet introuvable dans cette organisation"},
                )
            if agency_id and counter_branch != agency_id:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "COUNTER_BRANCH_MISMATCH",
                        "message": "Ce guichet n'appartient pas à l'agence indiquée",
                    },
                )
            agency_id = agency_id or counter_branch

        return agency_id, counter_id

    async def create(
        self, org_id: str, data: CreateEmployeeRequest
    ) -> EmployeeResponse:
        """
        Crée un employé en liant un User existant à une organisation.

        Args:
            org_id: ID de l'organisation (isolation multi-tenant)
            data: user_id, role, agency_id (optionnel), counter_id (optionnel)

        Returns:
            EmployeeResponse avec l'employé créé

        Raises:
            HTTPException 404: Si l'utilisateur n'existe pas
            HTTPException 409: Si l'utilisateur est déjà employé dans cette org
        """
        user = await self._verify_user_exists(data.user_id)

        # Un utilisateur déjà rattaché à une AUTRE organisation ne peut pas être
        # « récupéré » : sinon l'admin d'une org pourrait détourner le compte d'une autre.
        if user.org_id is not None and user.org_id != org_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "USER_IN_OTHER_ORG",
                    "message": "Cet utilisateur appartient déjà à une autre organisation",
                },
            )

        # L'agence et le guichet doivent appartenir à CETTE organisation et être cohérents
        agency_id, counter_id = await self._validate_assignment(org_id, data.agency_id, data.counter_id)

        # Vérifier qu'il n'est pas déjà employé dans cette org
        existing = await self.db.execute(
            select(Employee).where(
                Employee.user_id == data.user_id,
                Employee.org_id == org_id,
                Employee.status != EmployeeStatus.INACTIVE,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "EMPLOYEE_EXISTS",
                    "message": "Cet utilisateur est déjà enregistré comme employé dans cette organisation",
                }
            )

        employee = Employee(
            org_id=org_id,
            user_id=data.user_id,
            agency_id=agency_id,
            counter_id=counter_id,
            role=data.role,
        )
        self.db.add(employee)

        # Mettre à jour le rôle du User pour refléter son nouveau rôle
        user.role = data.role
        user.org_id = org_id

        await self.db.commit()
        await self.db.refresh(employee)
        return EmployeeResponse.model_validate(employee)

    async def get_by_id(
        self, employee_id: str, org_id: str
    ) -> EmployeeResponse:
        """
        Récupère un employé par son ID.

        Args:
            employee_id: UUID de l'employé
            org_id: Isolation multi-tenant

        Returns:
            EmployeeResponse

        Raises:
            HTTPException 404: Si l'employé n'existe pas dans l'org
        """
        result = await self.db.execute(
            select(Employee).where(
                Employee.id == employee_id,
                Employee.org_id == org_id,
            )
        )
        employee = result.scalar_one_or_none()
        if not employee:
            raise HTTPException(
                status_code=404,
                detail={"code": "EMPLOYEE_NOT_FOUND", "message": "Employé introuvable"}
            )
        return EmployeeResponse.model_validate(employee)

    async def list(
        self,
        org_id: str,
        agency_id: str | None = None,
        role: UserRole | None = None,
        page: int = 1,
        page_size: int = 20,
        agency_ids: set[str] | None = None,
    ) -> EmployeeListResponse:
        """
        Liste les employés d'une organisation avec filtres optionnels.

        Args:
            org_id: Isolation multi-tenant obligatoire
            agency_id: Filtrer par agence (optionnel)
            role: Filtrer par rôle (optionnel)
            page: Numéro de page
            page_size: Éléments par page (max 100)
            agency_ids: Restreint aux employés de ces agences (admin d'agence : ses seules branches)

        Returns:
            EmployeeListResponse avec items et métadonnées de pagination
        """
        page_size = min(page_size, 100)
        offset = (page - 1) * page_size

        query = select(Employee).where(Employee.org_id == org_id)
        count_query = select(func.count(Employee.id)).where(Employee.org_id == org_id)

        if agency_ids is not None:
            query = query.where(Employee.agency_id.in_(agency_ids))
            count_query = count_query.where(Employee.agency_id.in_(agency_ids))

        if agency_id:
            query = query.where(Employee.agency_id == agency_id)
            count_query = count_query.where(Employee.agency_id == agency_id)

        if role:
            query = query.where(Employee.role == role)
            count_query = count_query.where(Employee.role == role)

        # Exclure les inactifs par défaut
        query = query.where(Employee.status != EmployeeStatus.INACTIVE)
        count_query = count_query.where(Employee.status != EmployeeStatus.INACTIVE)

        query = query.order_by(Employee.created_at.desc()).offset(offset).limit(page_size)

        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        result = await self.db.execute(query)
        employees = result.scalars().all()

        return EmployeeListResponse(
            items=[EmployeeResponse.model_validate(e) for e in employees],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def update(
        self, employee_id: str, org_id: str, data: UpdateEmployeeRequest
    ) -> EmployeeResponse:
        """
        Met à jour le rôle, l'affectation ou le statut d'un employé.

        Args:
            employee_id: UUID de l'employé
            org_id: Isolation multi-tenant
            data: Champs à mettre à jour

        Returns:
            EmployeeResponse mis à jour

        Raises:
            HTTPException 404: Si l'employé n'existe pas dans l'org
        """
        result = await self.db.execute(
            select(Employee).where(
                Employee.id == employee_id,
                Employee.org_id == org_id,
            )
        )
        employee = result.scalar_one_or_none()
        if not employee:
            raise HTTPException(
                status_code=404,
                detail={"code": "EMPLOYEE_NOT_FOUND", "message": "Employé introuvable"}
            )

        update_data = data.model_dump(exclude_unset=True)

        # Une nouvelle affectation doit rester dans l'organisation et rester cohérente
        if "agency_id" in update_data or "counter_id" in update_data:
            new_agency = update_data["agency_id"] if "agency_id" in update_data else employee.agency_id
            new_counter = update_data["counter_id"] if "counter_id" in update_data else employee.counter_id
            agency_id, counter_id = await self._validate_assignment(org_id, new_agency, new_counter)
            update_data["agency_id"] = agency_id
            update_data["counter_id"] = counter_id

        # Si le rôle change, mettre à jour aussi le User
        if "role" in update_data:
            user_result = await self.db.execute(
                select(User).where(User.id == employee.user_id)
            )
            user = user_result.scalar_one_or_none()
            if user:
                user.role = update_data["role"]

        for field, value in update_data.items():
            setattr(employee, field, value)

        await self.db.commit()
        await self.db.refresh(employee)
        return EmployeeResponse.model_validate(employee)

    async def deactivate(self, employee_id: str, org_id: str) -> None:
        """
        Désactive un employé (soft delete).
        Remet le User au rôle CLIENT.

        Args:
            employee_id: UUID de l'employé
            org_id: Isolation multi-tenant

        Raises:
            HTTPException 404: Si l'employé n'existe pas dans l'org
        """
        result = await self.db.execute(
            select(Employee).where(
                Employee.id == employee_id,
                Employee.org_id == org_id,
            )
        )
        employee = result.scalar_one_or_none()
        if not employee:
            raise HTTPException(
                status_code=404,
                detail={"code": "EMPLOYEE_NOT_FOUND", "message": "Employé introuvable"}
            )

        employee.status = EmployeeStatus.INACTIVE

        # Remettre le User au rôle CLIENT
        user_result = await self.db.execute(
            select(User).where(User.id == employee.user_id)
        )
        user = user_result.scalar_one_or_none()
        if user:
            user.role = UserRole.CLIENT
            user.org_id = None

        await self.db.commit()
