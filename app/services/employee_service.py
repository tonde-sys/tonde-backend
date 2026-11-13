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

from app.models.employee import Employee, EmployeeStatus
from app.models.user import User, UserRole
from app.schemas.employee import (
    CreateEmployeeRequest,
    UpdateEmployeeRequest,
    EmployeeResponse,
    EmployeeListResponse,
)


# ── Rôles assignables, par rôle de l'appelant ────────────────────────────────
# Principe : on ne peut jamais attribuer un rôle supérieur ou égal au sien.
# SUPER_ADMIN est volontairement ABSENT de toutes les listes : ce rôle ne
# s'attribue pas via l'API (provisionnement manuel uniquement), sinon un admin
# d'agence pouvait se promouvoir administrateur de toute la plateforme.
ASSIGNABLE_ROLES: dict[UserRole, frozenset[UserRole]] = {
    UserRole.SUPER_ADMIN: frozenset({
        UserRole.AGENT,
        UserRole.SUPERVISOR,
        UserRole.ADMIN_AGENCY,
        UserRole.ADMIN_ORG,
    }),
    UserRole.ADMIN_ORG: frozenset({
        UserRole.AGENT,
        UserRole.SUPERVISOR,
        UserRole.ADMIN_AGENCY,
    }),
    UserRole.ADMIN_AGENCY: frozenset({
        UserRole.AGENT,
        UserRole.SUPERVISOR,
    }),
}


class EmployeeService:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ── Garde-fous de sécurité ────────────────────────────────────────────────
    def _assert_role_assignable(self, caller: User, target_role: UserRole) -> None:
        """
        Vérifie que l'appelant a le droit d'attribuer ce rôle.

        Args:
            caller: Utilisateur authentifié qui effectue l'opération
            target_role: Rôle que l'on cherche à attribuer

        Raises:
            HTTPException 403: Rôle non attribuable par cet appelant
        """
        allowed = ASSIGNABLE_ROLES.get(caller.role, frozenset())
        if target_role not in allowed:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "ROLE_NOT_ASSIGNABLE",
                    "message": (
                        f"Le rôle '{target_role.value}' ne peut pas être attribué "
                        f"par un '{caller.role.value}'. "
                        f"Rôles autorisés : {sorted(r.value for r in allowed) or 'aucun'}"
                    ),
                },
            )

    def _assert_user_can_be_enrolled(self, user: User, org_id: str) -> None:
        """
        Empêche l'absorption d'un utilisateur appartenant à une autre organisation.

        Sans ce contrôle, un admin d'agence pouvait ajouter comme "employé" un
        utilisateur d'une organisation concurrente : le service écrasait alors
        son role ET son org_id (prise de contrôle inter-tenant).

        Args:
            user: Utilisateur cible
            org_id: Organisation dans laquelle on veut l'enrôler

        Raises:
            HTTPException 403: La cible est un super-admin (compte protégé)
            HTTPException 409: La cible appartient déjà à une autre organisation
        """
        if user.role == UserRole.SUPER_ADMIN:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "SUPER_ADMIN_PROTECTED",
                    "message": "Un super-admin ne peut pas être enrôlé comme employé",
                },
            )

        if user.org_id and user.org_id != org_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "USER_IN_ANOTHER_ORG",
                    "message": (
                        "Cet utilisateur appartient déjà à une autre organisation. "
                        "Il doit d'abord la quitter avant de rejoindre celle-ci."
                    ),
                },
            )

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

    async def create(
        self, org_id: str, data: CreateEmployeeRequest, caller: User
    ) -> EmployeeResponse:
        """
        Crée un employé en liant un User existant à une organisation.

        Args:
            org_id: ID de l'organisation (isolation multi-tenant)
            data: user_id, role, agency_id (optionnel), counter_id (optionnel)
            caller: Utilisateur authentifié — détermine les rôles attribuables

        Returns:
            EmployeeResponse avec l'employé créé

        Raises:
            HTTPException 403: Rôle non attribuable ou cible protégée
            HTTPException 404: Si l'utilisateur n'existe pas
            HTTPException 409: Déjà employé dans cette org, ou user d'une autre org
        """
        self._assert_role_assignable(caller, data.role)

        user = await self._verify_user_exists(data.user_id)
        self._assert_user_can_be_enrolled(user, org_id)

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
            **data.model_dump(),
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
    ) -> EmployeeListResponse:
        """
        Liste les employés d'une organisation avec filtres optionnels.

        Args:
            org_id: Isolation multi-tenant obligatoire
            agency_id: Filtrer par agence (optionnel)
            role: Filtrer par rôle (optionnel)
            page: Numéro de page
            page_size: Éléments par page (max 100)

        Returns:
            EmployeeListResponse avec items et métadonnées de pagination
        """
        page_size = min(page_size, 100)
        offset = (page - 1) * page_size

        query = select(Employee).where(Employee.org_id == org_id)
        count_query = select(func.count(Employee.id)).where(Employee.org_id == org_id)

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
        self,
        employee_id: str,
        org_id: str,
        data: UpdateEmployeeRequest,
        caller: User,
    ) -> EmployeeResponse:
        """
        Met à jour le rôle, l'affectation ou le statut d'un employé.

        Args:
            employee_id: UUID de l'employé
            org_id: Isolation multi-tenant
            data: Champs à mettre à jour
            caller: Utilisateur authentifié — détermine les rôles attribuables

        Returns:
            EmployeeResponse mis à jour

        Raises:
            HTTPException 403: Rôle non attribuable ou employé super-admin
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
        new_role = update_data.get("role")

        # Charger le User associé une seule fois — utile pour les deux contrôles
        user_result = await self.db.execute(
            select(User).where(User.id == employee.user_id)
        )
        user = user_result.scalar_one_or_none()

        # Un super-admin ne se modifie pas via l'API de gestion des employés
        if user and user.role == UserRole.SUPER_ADMIN:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "SUPER_ADMIN_PROTECTED",
                    "message": "Un super-admin ne peut pas être modifié via la gestion des employés",
                },
            )

        # Si le rôle change, vérifier qu'il est attribuable par l'appelant
        if new_role is not None:
            self._assert_role_assignable(caller, new_role)

        for field, value in update_data.items():
            setattr(employee, field, value)

        if new_role is not None and user:
            user.role = new_role

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
