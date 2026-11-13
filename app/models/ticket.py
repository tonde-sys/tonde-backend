"""
Modèle Ticket — Table 'tickets' dans PostgreSQL.
C'est le cœur du système TONDE.

Chaque ticket appartient à une organisation (org_id),
une agence (agency_id) et un service (service_id).
Les états et transitions sont définis ici ;
la machine à états est dans ticket_service.py.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    String, DateTime, Enum as SAEnum, Integer, ForeignKey, Boolean,
    UniqueConstraint, Index,
)
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base
import enum


class TicketStatus(str, enum.Enum):
    WAITING     = "waiting"      # En attente dans la file
    CALLED      = "called"       # Appelé par le guichetier
    SERVING     = "serving"      # En cours de service au guichet
    DONE        = "done"         # Service terminé avec succès
    ABSENT      = "absent"       # Client non-présent après timeout (3 min)
    TRANSFERRED = "transferred"  # Transféré vers un autre guichet/service
    CANCELLED   = "cancelled"    # Annulé par le client
    INCOMPLETE  = "incomplete"   # Service interrompu, non finalisé


class TicketPriority(str, enum.Enum):
    STANDARD  = "standard"   # File normale FIFO
    PRIORITY  = "priority"   # Femme enceinte, personne âgée, handicap
    VIP       = "vip"        # Abonnement premium payant
    EMERGENCY = "emergency"  # Urgence médicale


# Scores de priorité pour Redis Sorted Set
# Plus le score est bas, plus le ticket passe vite
PRIORITY_SCORES: dict[TicketPriority, int] = {
    TicketPriority.EMERGENCY: 9,
    TicketPriority.VIP:       5,
    TicketPriority.PRIORITY:  3,
    TicketPriority.STANDARD:  0,
}

# Transitions d'états autorisées — source de vérité pour la machine à états
# Toute transition absente de ce dict est INTERDITE
#
# Statuts « actifs » (DÉCISION 1) : WAITING, CALLED, SERVING, ABSENT, TRANSFERRED,
# INCOMPLETE bloquent la création d'un nouveau ticket. Chacun de ces statuts DOIT
# donc avoir une sortie, sinon le client serait bloqué pour toujours :
#   - ABSENT     → WAITING (le client revient) ou CANCELLED (fenêtre de retour expirée)
#   - TRANSFERRED → WAITING (état transitoire : le ticket rejoint la file du service cible)
#   - INCOMPLETE → WAITING (l'agent remet en file) ou CANCELLED (clôture)
ALLOWED_TRANSITIONS: dict[TicketStatus, list[TicketStatus]] = {
    TicketStatus.WAITING: [
        TicketStatus.CALLED,
        TicketStatus.CANCELLED,
    ],
    TicketStatus.CALLED: [
        TicketStatus.SERVING,
        TicketStatus.ABSENT,
        TicketStatus.TRANSFERRED,
    ],
    TicketStatus.SERVING: [
        TicketStatus.DONE,
        TicketStatus.INCOMPLETE,
        TicketStatus.TRANSFERRED,
    ],
    TicketStatus.ABSENT: [
        TicketStatus.WAITING,    # Le client demande à revenir en file
        TicketStatus.CANCELLED,  # Fenêtre de retour expirée
    ],
    TicketStatus.TRANSFERRED: [
        TicketStatus.WAITING,    # Rejoint la file du service cible
    ],
    TicketStatus.INCOMPLETE: [
        TicketStatus.WAITING,    # L'agent remet le client en file
        TicketStatus.CANCELLED,  # Clôture
    ],
    # États terminaux — aucune transition possible
    TicketStatus.DONE:        [],
    TicketStatus.CANCELLED:   [],
}


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        # Idempotence (offline-first) : un même client_action_id ne crée qu'un ticket par user
        UniqueConstraint("user_id", "client_action_id", name="uq_tickets_user_client_action"),
        Index("ix_tickets_user_status", "user_id", "status"),
        Index("ix_tickets_queue_lookup", "agency_id", "service_id", "status"),
    )

    # ── Identifiant unique ────────────────────────────────────
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )

    # ── Multi-tenant ──────────────────────────────────────────
    org_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        index=True,
    )

    # ── Numéro lisible (ex: B-145) ────────────────────────────
    number: Mapped[str] = mapped_column(String(20))
    prefix: Mapped[str] = mapped_column(String(5), default="A")
    sequence: Mapped[int] = mapped_column(Integer)

    # ── Relations ─────────────────────────────────────────────
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE")
    )
    agency_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("branches.id", ondelete="CASCADE")
    )
    service_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("services.id", ondelete="CASCADE")
    )

    # ── Statut et priorité ────────────────────────────────────
    status: Mapped[TicketStatus] = mapped_column(
        SAEnum(TicketStatus), default=TicketStatus.WAITING
    )
    # Priorité EFFECTIVE (celle qui détermine l'ordre dans la file).
    priority: Mapped[TicketPriority] = mapped_column(
        SAEnum(TicketPriority), default=TicketPriority.STANDARD
    )
    # Priorité DEMANDÉE par le client, en attente de confirmation par un agent.
    # Un client ne peut jamais s'attribuer lui-même une priorité : le ticket est
    # créé en STANDARD et l'agent approuve ou rejette la demande.
    requested_priority: Mapped[TicketPriority | None] = mapped_column(
        SAEnum(TicketPriority), nullable=True
    )

    # ── Guichet assigné ───────────────────────────────────────
    counter_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    counter_name: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # ── QR Code unique pour vérification physique ─────────────
    qr_token: Mapped[str] = mapped_column(
        String(100), unique=True,
        default=lambda: str(uuid.uuid4()),
    )

    # ── Estimations de temps ──────────────────────────────────
    estimated_wait_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Attente RÉELLE : de la création à l'appel (renseignée à l'appel du ticket)
    actual_wait_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Durée RÉELLE du service en secondes (served_at → done_at). Alimente l'ETA.
    service_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── Idempotence (offline-first) ───────────────────────────
    # Identifiant généré par le client pour l'action de création.
    client_action_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── Dates clés (timeline du ticket) ──────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    called_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    served_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    done_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Notifications envoyées ────────────────────────────────
    notified_5min: Mapped[bool] = mapped_column(Boolean, default=False)
    notified_turn: Mapped[bool] = mapped_column(Boolean, default=False)

    def __repr__(self) -> str:
        return f"<Ticket {self.number} — {self.status}>"
