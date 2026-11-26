"""
Schémas Pydantic v2 pour le module Organizations.
"""
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, field_validator, EmailStr


# ── Secteurs autorisés ────────────────────────────────────────────────────────
OrgSector = Literal["bank", "hospital", "university", "administration", "other"]


# ── Requêtes ──────────────────────────────────────────────────────────────────
class CreateOrganizationRequest(BaseModel):
    name: str
    slug: str
    sector: OrgSector
    description: str | None = None
    country: str = "Burundi"
    city: str | None = None
    phone: str | None = None
    email: EmailStr | None = None
    website: str | None = None
    support_whatsapp: str | None = None
    support_hours: str | None = None
    logo_url: str | None = None

    @field_validator("slug")
    @classmethod
    def slug_must_be_valid(cls, v: str) -> str:
        import re
        if not re.match(r"^[a-z0-9-]+$", v):
            raise ValueError("Le slug ne peut contenir que des lettres minuscules, chiffres et tirets")
        return v

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Le nom ne peut pas être vide")
        return v.strip()


class UpdateOrganizationRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    country: str | None = None
    city: str | None = None
    phone: str | None = None
    email: EmailStr | None = None
    website: str | None = None
    support_whatsapp: str | None = None
    support_hours: str | None = None
    logo_url: str | None = None
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("Le nom ne peut pas être vide")
        return v.strip() if v else v


# ── Réponses ──────────────────────────────────────────────────────────────────
class OrganizationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    slug: str
    sector: str
    description: str | None
    country: str
    city: str | None
    phone: str | None
    email: str | None
    website: str | None
    # Canaux de support publiés à l'application mobile (L6). Nullables : une
    # institution qui n'a rien renseigné n'affiche pas de bouton mort.
    support_whatsapp: str | None = None
    support_hours: str | None = None
    logo_url: str | None
    is_active: bool
    # S2-08 : champs invitation (None si pas de code généré)
    invitation_code: str | None = None
    invitation_expires_at: datetime | None = None
    invitation_code_active: bool = False
    created_at: datetime
    updated_at: datetime


class OrganizationListResponse(BaseModel):
    items: list[OrganizationResponse]
    total: int
    page: int
    page_size: int


# ── S2-08 : Join by Code ──────────────────────────────────────────────────────
class JoinByCodeRequest(BaseModel):
    invitation_code: str
    member_number: str | None = None  # numéro de compte, dossier patient, etc.

    @field_validator("invitation_code")
    @classmethod
    def code_uppercase(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("Le code d'invitation ne peut pas être vide")
        return v


class GenerateInvitationCodeRequest(BaseModel):
    expires_in_days: int = 30  # expiration par défaut 30 jours

    @field_validator("expires_in_days")
    @classmethod
    def valid_expiry(cls, v: int) -> int:
        if v < 1 or v > 365:
            raise ValueError("L'expiration doit être entre 1 et 365 jours")
        return v


class OrganizationMemberResponse(BaseModel):
    """Réponse après avoir rejoint une organisation."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    organization_id: str
    organization_name: str
    member_number: str | None
    status: str
    joined_at: datetime


class UserOrganizationListResponse(BaseModel):
    items: list[OrganizationMemberResponse]
    total: int
