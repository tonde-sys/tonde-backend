"""
Schémas Pydantic v2 pour le module Employees (Agents).
"""
from datetime import datetime
from pydantic import BaseModel, ConfigDict, field_validator
from app.models.user import UserRole
from app.models.employee import EmployeeStatus


# ── Requêtes ──────────────────────────────────────────────────────────────────
# Rôles qu'un client HTTP peut mentionner dans un corps de requête.
# SUPER_ADMIN est exclu au niveau du schéma (première barrière) : ce rôle ne
# s'attribue jamais via l'API, uniquement par provisionnement manuel.
# La seconde barrière (rôle de l'appelant vs rôle visé) est dans EmployeeService.
API_FORBIDDEN_ROLES = {UserRole.CLIENT, UserRole.SUPER_ADMIN}


class CreateEmployeeRequest(BaseModel):
    """Crée un employé en liant un User existant à une organisation."""
    user_id: str
    agency_id: str | None = None
    counter_id: str | None = None
    role: UserRole = UserRole.AGENT

    @field_validator("role")
    @classmethod
    def role_must_be_staff(cls, v: UserRole) -> UserRole:
        """Ni un CLIENT ni un SUPER_ADMIN ne peuvent être attribués par l'API."""
        if v in API_FORBIDDEN_ROLES:
            raise ValueError(
                f"Le rôle '{v.value}' ne peut pas être attribué via l'API. "
                "Rôles acceptés : agent, supervisor, admin_agency, admin_org."
            )
        return v


class UpdateEmployeeRequest(BaseModel):
    """Met à jour le rôle, l'affectation ou le statut d'un employé."""
    agency_id: str | None = None
    counter_id: str | None = None
    role: UserRole | None = None
    status: EmployeeStatus | None = None

    @field_validator("role")
    @classmethod
    def role_must_be_staff(cls, v: UserRole | None) -> UserRole | None:
        if v is not None and v in API_FORBIDDEN_ROLES:
            raise ValueError(
                f"Le rôle '{v.value}' ne peut pas être attribué via l'API. "
                "Rôles acceptés : agent, supervisor, admin_agency, admin_org."
            )
        return v


# ── Réponses ──────────────────────────────────────────────────────────────────
class EmployeeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    org_id: str
    user_id: str
    agency_id: str | None
    counter_id: str | None
    role: str
    status: str
    created_at: datetime
    updated_at: datetime


class EmployeeListResponse(BaseModel):
    items: list[EmployeeResponse]
    total: int
    page: int
    page_size: int
