"""
Types d'événements WebSocket officiels de TONDE.

Tous les messages WebSocket envoyés aux clients doivent
utiliser ces types. C'est le contrat entre le backend
et les clients (Flutter mobile, Vue.js dashboard).

Architecture :
  Queue Engine (service) → publie événement
  WS Manager             → diffuse aux clients connectés

Les deux sont découplés : le service ne connaît pas les connexions,
le WS Manager ne connaît pas la logique métier.
"""
from enum import Enum
from typing import Any
from pydantic import BaseModel


class EventType(str, Enum):
    """Types d'événements WebSocket officiels."""

    # ── Événements ticket ────────────────────────────────────
    TICKET_CALLED      = "ticket_called"      # Un numéro est appelé (broadcast agence)
    TICKET_ABSENT      = "ticket_absent"      # Client non-présent (notif personnelle)
    TICKET_TRANSFERRED = "ticket_transferred" # Ticket transféré (notif personnelle)
    TICKET_DONE        = "ticket_done"        # Service terminé (notif personnelle)

    # ── Événements file ──────────────────────────────────────
    QUEUE_UPDATE       = "queue_update"       # Position/ETA mis à jour (notif personnelle)
    YOUR_TURN          = "your_turn"          # C'est votre tour ! (notif personnelle)
    ABSENT_WARNING     = "absent_warning"     # Avertissement avant absent (notif personnelle)

    # ── Événements guichet ───────────────────────────────────
    COUNTER_OPEN       = "counter_open"       # Guichet ouvert (broadcast agence)
    COUNTER_CLOSE      = "counter_close"      # Guichet fermé (broadcast agence)
    COUNTER_PAUSE      = "counter_pause"      # Guichet en pause (broadcast agence)

    # ── Événements internes / priorité ───────────────────────
    # QUEUE_SNAPSHOT circule UNIQUEMENT entre instances (via Redis) : il contient
    # les positions de toute une file. Chaque instance le transforme en un
    # QUEUE_UPDATE individuel par client connecté — un client ne reçoit jamais
    # les informations des autres.
    QUEUE_SNAPSHOT     = "queue_snapshot"
    PRIORITY_UPDATED   = "priority_updated"   # Priorité confirmée/rejetée par un agent (personnel)

    # ── Événements système ───────────────────────────────────
    BROADCAST_MESSAGE  = "broadcast_message"  # Message général (broadcast agence)
    CONNECTED          = "connected"          # Confirmation connexion
    PONG               = "pong"               # Réponse au ping de keepalive


class WebSocketEvent(BaseModel):
    """
    Structure de base de tous les messages WebSocket TONDE.
    Tout message envoyé doit respecter ce format.
    """
    type: EventType
    data: dict[str, Any] = {}


class TicketCalledEvent(BaseModel):
    """Diffusé à toute la salle quand un numéro est appelé."""
    type: EventType = EventType.TICKET_CALLED
    called_number: str          # Ex: "B-145"
    counter_name: str           # Ex: "Guichet 3"
    agency_id: str


class YourTurnEvent(BaseModel):
    """Envoyé uniquement au client dont le ticket vient d'être appelé."""
    type: EventType = EventType.YOUR_TURN
    ticket_id: str
    ticket_number: str          # Ex: "B-145"
    counter_name: str           # Ex: "Guichet 3"
    message: str                # Ex: "Présentez-vous au Guichet 3"
    timeout_seconds: int = 180  # 3 minutes pour se présenter


class QueueUpdateEvent(BaseModel):
    """Envoyé périodiquement pour mettre à jour la position d'un client."""
    type: EventType = EventType.QUEUE_UPDATE
    ticket_id: str
    ticket_number: str
    position: int               # Position actuelle (1 = prochain)
    total_in_queue: int         # Nombre total en attente
    estimated_wait_minutes: int
    current_called_number: str  # Numéro actuellement au guichet


class BroadcastMessageEvent(BaseModel):
    """Message général envoyé à toute la salle (info, alerte, fermeture)."""
    type: EventType = EventType.BROADCAST_MESSAGE
    agency_id: str
    message: str
    level: str = "info"         # info | warning | urgent


def make_connected_event() -> dict:
    """Retourne le message de confirmation de connexion."""
    return {"type": EventType.CONNECTED, "message": "Connexion établie"}


def make_pong_event() -> dict:
    """Retourne la réponse pong au keepalive ping."""
    return {"type": EventType.PONG}
