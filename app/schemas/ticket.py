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

    # ── Contexte d'affichage ─────────────────────────────────
    # Noms lisibles du triplet (organisation, agence, service).
    #
    # Facultatifs à dessein : `get_history` renvoie une PAGE de tickets, et
    # joindre trois tables par ligne transformerait une requête en N+1. Les
    # chemins qui servent UN ticket (création, détail, ticket actif) les
    # remplissent — ce sont ceux que le mobile affiche.
    #
    # Sans eux, le mobile ne pouvait afficher que des UUID : il n'a aucun moyen
    # de traduire `agency_id` en nom, et `/history` est paginé (le ticket actif
    # peut ne pas être sur la première page).
    organization_id: Optional[str] = None
    organization_name: Optional[str] = None
    agency_name: Optional[str] = None
    service_name: Optional[str] = None

    qr_token: str                            # Pour le QR Code affiché sur le mobile
    position: int                            # Position actuelle dans la file (0 si hors file)
    total_in_queue: int                      # Nombre total de tickets en file
    estimated_wait_minutes: Optional[int] = None
    estimated_wait_confidence: Optional[str] = None
    eta_calculated_at: Optional[datetime] = None
    eta_version: Optional[int] = None
    created_at: datetime
    counter_name: Optional[str] = None       # Guichet assigné si appelé

    # ── Horodatages du cycle de vie ───────────────────────────
    # Exposés pour que le poste guichet calcule la durée de service réelle
    # (served_at → done_at) au lieu de la déduire de l'instant du fetch.
    # Sans eux, après une reprise/reconnexion, un ticket déjà en service
    # apparaît comme venant d'être appelé et la durée affichée est fausse.
    called_at: Optional[datetime] = None
    served_at: Optional[datetime] = None
    done_at: Optional[datetime] = None

    # ── Absence et reprise (spécification Queue Engine §5.1) ──
    # Le poste guichet a besoin de ces trois valeurs pour AGIR, pas pour décider :
    #   - `grace_expires_at` : quand le bouton « Client absent » devient actif.
    #     Sans elle, le desktop devrait recalculer `called_at + 90 s` et se
    #     tromperait dès que la valeur change côté serveur ;
    #   - `recovery_expires_at` : ce qu'on affiche au client (« revenez avant
    #     10h32 ») ;
    #   - `missed_count` : ce qui reste comme chances avant la clôture.
    # Ce sont des valeurs SERVEUR exposées, jamais des valeurs que le client
    # recalcule ou dont il déduit une autorisation.
    grace_expires_at: Optional[datetime] = None
    recovery_expires_at: Optional[datetime] = None
    missed_count: int = 0

    # ── File dégradée (spécification §7.2) ───────────────────
    # Vrai quand le ticket existe en base mais n'a pas pu être inséré dans la
    # file Redis : `position` vaut alors 0 (« hors file ») et le scheduler le
    # remettra en place au retour de Redis. Le client doit le savoir pour ne pas
    # afficher une position qui n'existe pas.
    queue_degraded: bool = False

    # Ordre de passage monotone (spécification §4.2). Le client s'en sert pour
    # trier sans recalculer un ordre à partir des horodatages — ce que le desktop
    # faisait, et qui ne reproduisait pas exactement l'ordre du serveur.
    queue_sequence: Optional[int] = None

    # ── Identité du client (anti-fraude) ──────────────────────
    # Affichés au guichetier lors de l'appel pour vérifier que la personne
    # présente correspond à celle qui a pris le ticket. Renseignés uniquement
    # pour les routes consultées par le staff (get_ticket, call_next).
    # Le mobile client ne les reçoit pas (ils ne sont pas peuplés dans les
    # réponses client — _fill_client_identity n'est appelé que côté staff).
    client_name: Optional[str] = None
    client_phone: Optional[str] = None


class TicketHistoryResponse(BaseModel):
    """Réponse paginée pour l'historique des tickets."""
    model_config = ConfigDict(from_attributes=True)

    items: list[TicketResponse]
    total: int          # Nombre total de tickets (toutes pages)
    page: int           # Page actuelle
    page_size: int      # Taille de la page
    has_next: bool      # true si une page suivante existe


class QueueEntry(BaseModel):
    """
    Une ligne de la file d'attente vue par le staff (constat P1-8).

    Volontairement plus légère que `TicketResponse` : le poste guichet a besoin
    d'ordonner, d'appeler et de repérer un ticket fantôme — pas de rejouer tout
    le cycle de vie. Pas de `qr_token` non plus : un agent n'a pas à scanner le
    QR d'un client, et l'exposer élargirait inutilement la surface.
    """
    ticket_id: str
    number: str                                  # Ex: "C-005"
    status: str                                  # waiting | called | serving
    priority: str                                # Priorité effective
    requested_priority: Optional[str] = None     # Demande non encore confirmée
    position: int                                # Rang dans la file (1 = prochain)
    waiting_since_minutes: int                   # Ancienneté dans l'état courant
    created_at: datetime
    counter_name: Optional[str] = None           # Guichet, si appelé/en service


class QueueListResponse(BaseModel):
    """File d'un service, ordonnée comme le sera l'appel du guichetier."""
    agency_id: str
    service_id: str
    service_name: Optional[str] = None
    waiting_count: int                    # Tickets WAITING
    called_count: int                     # Tickets CALLED (appelés, non présentés)
    serving_count: int                    # Tickets SERVING (en cours)
    average_wait_minutes: Optional[int] = None
    entries: list[QueueEntry]             # WAITING d'abord, puis CALLED, puis SERVING
    generated_at: datetime


class QueueUpdateEvent(BaseModel):
    """Structure des messages WebSocket envoyés au client mobile."""
    type: str                     # "queue_update", "your_turn", "absent_warning"
    ticket_id: str
    ticket_number: str
    current_called_number: str    # Numéro actuellement servi dans la salle (harmonisé avec events.py)
    position: int
    total_in_queue: int
    estimated_wait_minutes: Optional[int] = None
    estimated_wait_confidence: Optional[str] = None
    eta_calculated_at: Optional[datetime] = None
    eta_version: Optional[int] = None
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
