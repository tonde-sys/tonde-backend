"""
Schémas Pydantic v2 pour les tickets.
Séparés des modèles SQLAlchemy — ce sont les contrats de l'API.
"""
import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator

VALID_PRIORITIES = {"standard", "priority", "vip", "emergency"}
_CLIENT_ACTION_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")


class CreateTicketRequest(BaseModel):
    agency_id: str
    service_id: str
    # Priorité DEMANDÉE. Elle n'est jamais appliquée directement pour un client :
    # le ticket est créé en « standard » et un agent confirme la demande au guichet.
    priority: str = "standard"
    # Identifiant d'idempotence généré par le client (UUID recommandé). Renvoyer la même
    # requête (réseau instable) retourne le ticket déjà créé au lieu d'en créer un second.
    client_action_id: Optional[str] = None

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, v: str) -> str:
        if v not in VALID_PRIORITIES:
            raise ValueError(f"Priorité invalide. Valeurs acceptées : {', '.join(sorted(VALID_PRIORITIES))}")
        return v

    @field_validator("client_action_id")
    @classmethod
    def validate_client_action_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not _CLIENT_ACTION_ID_RE.match(v):
            raise ValueError("client_action_id doit contenir 8 à 64 caractères [A-Za-z0-9_-]")
        return v


class TicketResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    number: str                              # Ex: "B-145"
    status: str                              # waiting, called, serving, done...
    priority: str                            # Priorité effective : standard, priority, vip, emergency
    requested_priority: Optional[str] = None  # Priorité demandée, en attente de confirmation par un agent
    agency_id: str
    service_id: str
    qr_token: str                            # Pour le QR Code affiché sur le mobile
    position: int                            # Position actuelle dans la file (0 si hors file)
    total_in_queue: int                      # Nombre total de tickets en file
    estimated_wait_minutes: Optional[int] = None
    created_at: datetime
    counter_name: Optional[str] = None       # Guichet assigné si appelé


class TicketHistoryResponse(BaseModel):
    """Réponse paginée pour l'historique des tickets."""
    model_config = ConfigDict(from_attributes=True)

    items: list[TicketResponse]
    total: int          # Nombre total de tickets (toutes pages)
    page: int           # Page actuelle
    page_size: int      # Taille de la page
    has_next: bool      # true si une page suivante existe


class QueueUpdateEvent(BaseModel):
    """Structure des messages WebSocket envoyés au client mobile."""
    type: str                     # "queue_update", "your_turn", "absent_warning"
    ticket_id: str
    ticket_number: str
    current_called_number: str    # Numéro actuellement servi dans la salle (harmonisé avec events.py)
    position: int
    total_in_queue: int
    estimated_wait_minutes: int
    counter_name: Optional[str] = None


class CallNextRequest(BaseModel):
    """Requête du guichetier pour appeler le prochain ticket."""
    agency_id: str
    service_id: str   # File à cibler — chaque service a sa propre file Redis
    counter_id: str
    # Ignoré par le serveur : le nom affiché vient de la base (Counter.name).
    # Champ conservé optionnel pour rester compatible avec les clients existants.
    counter_name: str | None = None


class TransferTicketRequest(BaseModel):
    """Transfert d'un ticket appelé/en service vers un autre service de la même branch."""
    target_service_id: str
    reason: str | None = None

    @field_validator("reason")
    @classmethod
    def reason_max_length(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            if len(v) > 200:
                raise ValueError("Le motif ne doit pas dépasser 200 caractères")
        return v or None


class SetPriorityRequest(BaseModel):
    """
    Décision d'un agent sur la priorité d'un ticket en attente.

    - approve=True, priority omis   → accepte la priorité demandée par le client
    - approve=True, priority fourni → attribue cette priorité (même sans demande)
    - approve=False                 → rejette la demande du client (reste en standard)
    """
    approve: bool = True
    priority: str | None = None

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_PRIORITIES:
            raise ValueError(f"Priorité invalide. Valeurs acceptées : {', '.join(sorted(VALID_PRIORITIES))}")
        return v


class CloseTicketRequest(BaseModel):
    """Clôture d'un ticket par un agent (ticket abandonné, absent, interrompu)."""
    reason: str | None = None

    @field_validator("reason")
    @classmethod
    def reason_max_length(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            if len(v) > 200:
                raise ValueError("Le motif ne doit pas dépasser 200 caractères")
        return v or None
