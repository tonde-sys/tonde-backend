"""
Schémas Pydantic v2 pour le module Counters (Guichets).
"""
from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict, field_validator


# ── Requêtes ──────────────────────────────────────────────────────────────────
class CreateCounterRequest(BaseModel):
    name: str
    description: str | None = None

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Le nom du guichet ne peut pas être vide")
        return v.strip()


class UpdateCounterRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("Le nom du guichet ne peut pas être vide")
        return v.strip() if v else v


class PauseCounterRequest(BaseModel):
    """Pause d'un guichet avec une heure de reprise annoncée aux clients.

    `paused_until` est optionnel : sans heure, le client voit seulement que le
    guichet est en pause. Une date naive est interprétée en UTC, comme le reste
    de l'API ; les clients envoient idéalement une date ISO avec décalage.
    """

    paused_until: datetime | None = None

    @field_validator("paused_until")
    @classmethod
    def pause_end_must_be_in_future(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        if value <= datetime.now(timezone.utc):
            raise ValueError("L'heure de reprise doit être dans le futur")
        return value.astimezone(timezone.utc)


class AssignAgentRequest(BaseModel):
    """Assigne un agent à ce guichet."""
    employee_id: str


# ── Réponses ──────────────────────────────────────────────────────────────────
class CounterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    org_id: str
    agency_id: str
    name: str
    description: str | None
    is_open: bool
    is_active: bool
    is_paused: bool = False
    paused_until: datetime | None = None
    current_ticket_id: str | None
    created_at: datetime
    updated_at: datetime


class CounterListResponse(BaseModel):
    items: list[CounterResponse]
    total: int
    page: int
    page_size: int
