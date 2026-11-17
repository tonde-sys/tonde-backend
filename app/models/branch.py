"""
Modèles Branch et Service — Sprint 2 (renommage Agency → Branch).

TONDE hiérarchie officielle :
  Organization → Branch → Service → Counter → Ticket

Alembic : migration 003_rename_agencies_to_branches.py
  → ALTER TABLE agencies RENAME TO branches
  → ALTER TABLE services : FK agency_id pointe toujours sur la même table renommée
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, Float, Boolean, Integer, ForeignKey, Text, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base


class Branch(Base):
    __table_args__ = (UniqueConstraint("id", "org_id", name="uq_branches_id_org_qape"),)
    """
    Filiale / agence physique d'une organisation.

    Exemples : BANCOBU Agence Centre-Ville, CHUK Urgences.
    Contient des Services (Caisse, Crédit, Consultation…).
    """
    __tablename__ = "branches"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )

    # ── Multi-tenant ──────────────────────────────────────────
    org_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # ── Informations de base ──────────────────────────────────
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Valeurs : bank, hospital, university, administration, other
    sector: Mapped[str] = mapped_column(String(50))

    # ── Contact ───────────────────────────────────────────────
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    city: Mapped[str] = mapped_column(String(100), default="Bujumbura")
    country: Mapped[str] = mapped_column(String(50), default="Burundi")

    # ── Géolocalisation ───────────────────────────────────────
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ── Horaires ──────────────────────────────────────────────
    opens_at: Mapped[str] = mapped_column(String(5), default="08:00")
    closes_at: Mapped[str] = mapped_column(String(5), default="17:00")

    # ── Branding ──────────────────────────────────────────────
    logo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # ── Configuration file d'attente ──────────────────────────
    max_daily_tickets: Mapped[int] = mapped_column(Integer, default=500)
    avg_service_minutes: Mapped[int] = mapped_column(Integer, default=5)

    # ── Configuration avancée (S2-10) ─────────────────────────
    # Seuil d'alerte : notifier si temps d'attente > N minutes
    max_wait_minutes_alert: Mapped[int] = mapped_column(Integer, default=30)
    # Horaires détaillés par jour (JSON) — ex: {"monday": {"open": "08:00", "close": "17:00"}}
    # Si NULL, on utilise opens_at/closes_at comme horaire global
    operating_hours: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Activer les rappels SMS avant le tour du client
    enable_sms_reminders: Mapped[bool] = mapped_column(Boolean, default=False)
    # Intervalle de rappel SMS en minutes avant appel estimé
    reminder_interval_minutes: Mapped[int] = mapped_column(Integer, default=10)
    # Langues supportées par cette branch (JSON array) — ex: ["fr", "en"]
    supported_languages: Mapped[list | None] = mapped_column(JSON, nullable=True, default=lambda: ["fr"])

    # ── Statut ────────────────────────────────────────────────
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_open: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── Dates ─────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # ── Relations ─────────────────────────────────────────────
    services: Mapped[list["Service"]] = relationship(
        "Service", back_populates="branch", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Branch {self.name}>"


class Service(Base):
    """
    Un service proposé par une branch.
    Exemples : 'Dépôt', 'Retrait', 'Consultation médicale', 'Inscription'.
    Chaque service a son propre préfixe de ticket (A, B, C...).
    """
    __tablename__ = "services"
    # Parent key for QAPE's composite service/organization foreign keys.
    __table_args__ = (UniqueConstraint("id", "org_id", name="uq_services_id_org_qape"),)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )

    # ── Multi-tenant ──────────────────────────────────────────
    org_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    branch_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("branches.id", ondelete="CASCADE"),
        index=True,
    )

    # ── Informations ──────────────────────────────────────────
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Préfixe du ticket pour ce service (A, B, C…)
    ticket_prefix: Mapped[str] = mapped_column(String(5), default="A")

    # Durée moyenne d'un service en minutes (pour calcul ETA)
    avg_duration_minutes: Mapped[int] = mapped_column(Integer, default=5)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── Dates ─────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    # ── Relations ─────────────────────────────────────────────
    branch: Mapped["Branch"] = relationship("Branch", back_populates="services")

    def __repr__(self) -> str:
        return f"<Service {self.name} @ {self.branch_id}>"
