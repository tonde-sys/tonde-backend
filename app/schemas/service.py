"""
Schémas Pydantic v2 pour le module Services (files d'une branch).

Remplacent le `dict` brut que le router acceptait : aucune validation, donc un nom
vide, un préfixe de 50 caractères ou une durée négative passaient jusqu'à la base.
"""
import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

_PREFIX_RE = re.compile(r"^[A-Za-z]{1,5}$")


class CreateServiceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    # Lettre(s) imprimée(s) devant le numéro du ticket : A-001, B-014… (1 à 5 lettres)
    ticket_prefix: str = "A"
    # Durée moyenne par défaut d'un service (minutes) — sert à l'ETA tant que l'historique est court
    avg_duration_minutes: int = Field(default=5, ge=1, le=240)
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Le nom du service ne peut pas être vide")
        return v

    @field_validator("ticket_prefix")
    @classmethod
    def validate_prefix(cls, v: str) -> str:
        v = v.strip()
        if not _PREFIX_RE.match(v):
            raise ValueError("Le préfixe doit contenir 1 à 5 lettres (A-Z)")
        return v.upper()


class UpdateServiceRequest(BaseModel):
    """
    Mise à jour partielle. Le préfixe n'est volontairement PAS modifiable : changer
    la lettre d'un service en cours de journée créerait des numéros incohérents.
    """
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    avg_duration_minutes: int | None = Field(default=None, ge=1, le=240)
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("Le nom du service ne peut pas être vide")
        return v


class ServiceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    org_id: str | None
    branch_id: str
    name: str
    description: str | None
    ticket_prefix: str
    avg_duration_minutes: int
    is_active: bool
    created_at: datetime | None
