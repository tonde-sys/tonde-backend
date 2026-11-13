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
from sqlalchemy import String, DateTime, Enum as SAEnum, Integer, ForeignKey, Boolean
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
    ],
    TicketStatus.ABSENT: [
        TicketStatus.WAITING,     # Le client demande à revenir en file
        TicketStatus.CANCELLED,   # …ou renonce : sans cette sortie, un client
                                  # absent restait bloqué (ABSENT est « actif »)
    ],
    # États terminaux — aucune transition possible
    TicketStatus.DONE:        [],
    TicketStatus.TRANSFERRED: [],
    TicketStatus.CANCELLED:   [],
    TicketStatus.INCOMPLETE:  [],
}


class Ticket(Base):
    __tablename__ = "tickets"

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
    priority: Mapped[TicketPriority] = mapped_column(
        SAEnum(TicketPriority), default=TicketPriority.STANDARD
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
    actual_wait_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

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
