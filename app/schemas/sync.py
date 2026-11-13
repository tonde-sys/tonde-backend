"""
Schémas Pydantic v2 pour la synchronisation hors-ligne (offline-first).

Principe « optimistic UI » : le mobile applique l'action localement, l'affiche tout
de suite, puis l'envoie quand le réseau revient. Le serveur répond action par action
(appliquée, déjà appliquée, conflit ou refusée) pour que le mobile réconcilie son état.
"""
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.core.config import settings

_ACTION_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")

ActionType = Literal["create_ticket", "cancel_ticket", "return_to_queue"]
ActionStatus = Literal["applied", "already_applied", "conflict", "rejected"]


class SyncAction(BaseModel):
    """Une action effectuée hors-ligne par le client."""
    client_action_id: str   # Identifiant unique généré par le client : clé d'idempotence
    type: ActionType
    payload: dict[str, Any] = Field(default_factory=dict)
    client_created_at: datetime | None = None   # Quand l'utilisateur a fait l'action (heure du mobile)

    @field_validator("client_action_id")
    @classmethod
    def validate_action_id(cls, v: str) -> str:
        if not _ACTION_ID_RE.match(v):
            raise ValueError("client_action_id doit contenir 8 à 64 caractères [A-Za-z0-9_-]")
        return v


class SyncActionsRequest(BaseModel):
    actions: list[SyncAction] = Field(min_length=1, max_length=settings.SYNC_MAX_ACTIONS)

    @field_validator("actions")
    @classmethod
    def unique_ids(cls, v: list[SyncAction]) -> list[SyncAction]:
        ids = [a.client_action_id for a in v]
        if len(ids) != len(set(ids)):
            raise ValueError("client_action_id en double dans la même requête")
        return v


class SyncActionResult(BaseModel):
    client_action_id: str
    type: ActionType
    status: ActionStatus
    # Code d'erreur stable (TICKET_ALREADY_ACTIVE, INVALID_TRANSITION…) pour conflits/refus
    code: str | None = None
    message: str | None = None
    # État serveur du ticket concerné après traitement (le client remplace son état local)
    ticket: dict[str, Any] | None = None


class SyncActionsResponse(BaseModel):
    success: bool = True
    results: list[SyncActionResult]
    server_time: datetime
