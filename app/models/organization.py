"""
Modèle Organization — Table 'organizations' dans PostgreSQL.

Une Organization est la racine du multi-tenant dans TONDE.
Exemples : Banque Coopec Burundi, CHU Kamenge, Université du Burundi.

Hiérarchie complète :
  Organization → Branch → Service → Counter → Ticket
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base


class Organization(Base):
    __tablename__ = "organizations"

    # ── Identifiant ───────────────────────────────────────────
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )

    # ── Identité ──────────────────────────────────────────────
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Secteur d'activité ────────────────────────────────────
    # Valeurs attendues : bank, hospital, university, administration, other
    sector: Mapped[str] = mapped_column(String(50))

    # ── Localisation ─────────────────────────────────────────
    country: Mapped[str] = mapped_column(String(50), default="Burundi")
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # ── Contact ───────────────────────────────────────────────
    # `phone` et `email` servent AUSSI de canaux de support : ce sont les
    # coordonnées que l'institution publie pour être joignable. Les dupliquer
    # dans des colonnes `support_*` aurait créé deux vérités, et un client qui
    # appelle un numéro que l'institution a changé ailleurs.
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # ── Support client (L6) ───────────────────────────────────
    # WhatsApp est distinct du téléphone : beaucoup d'institutions burundaises
    # répondent sur WhatsApp et pas au téléphone. `support_hours` est du texte
    # libre (« Lun–Ven 8h–16h », « 24/7 ») : un champ structuré aurait imposé
    # un modèle de plages horaires que personne n'aurait rempli correctement.
    support_whatsapp: Mapped[str | None] = mapped_column(String(20), nullable=True)
    support_hours: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # ── Logo ──────────────────────────────────────────────────
    logo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # ── Statut ────────────────────────────────────────────────
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── Code d'invitation (S2-08) ─────────────────────────────
    # Code alphanumérique uppercase généré par l'admin pour permettre
    # aux utilisateurs de rejoindre l'organisation sans intervention manuelle.
    # Sécurité : code unique, expiration obligatoire, révocable à tout moment.
    invitation_code: Mapped[str | None] = mapped_column(
        String(12), unique=True, nullable=True, index=True
    )
    invitation_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    invitation_code_active: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

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
    # Sprint 2 — S2-01 : Agency renommée en Branch
    branches: Mapped[list["Branch"]] = relationship(  # noqa: F821
        "Branch", foreign_keys="Branch.org_id", cascade="all, delete-orphan"
    )
    # Sprint 2 — S2-02 : membres de l'organisation (table pivot)
    user_organizations: Mapped[list["UserOrganization"]] = relationship(  # noqa: F821
        "UserOrganization", back_populates="organization", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Organization {self.name} ({self.sector})>"
