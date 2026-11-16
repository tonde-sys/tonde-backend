"""
Modèle AuditLog — Table 'audit_logs' dans PostgreSQL.

Journal d'audit des actions SENSIBLES qui ne sont pas des transitions de ticket :
exports de données, décisions de priorité, etc.

Pourquoi une table séparée de queue_logs : queue_logs décrit le cycle de vie
d'un ticket (ticket_id obligatoire). Un export de données ou un changement de
priorité décidé par un agent n'est rattaché à aucune transition.

Règle : ces lignes ne sont jamais modifiées ni supprimées.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AuditAction:
    """Actions auditées (chaînes stables, pas un enum PostgreSQL : extensible sans migration)."""
    TICKET_EXPORTED = "ticket_exported"
    STATS_EXPORTED = "stats_exported"
    PRIORITY_APPROVED = "priority_approved"
    PRIORITY_REJECTED = "priority_rejected"
    PRIORITY_SET = "priority_set"
    TICKET_TRANSFERRED = "ticket_transferred"
    TICKET_REQUEUED = "ticket_requeued"
    TICKET_EXPIRED = "ticket_expired"
    # Relance de l'annonce d'un ticket appelé. Tracée parce qu'elle envoie un SMS
    # (coût réel) et parce qu'un agent qui relance dix fois le même client est un
    # signal d'exploitation, pas un détail.
    TICKET_RECALLED = "ticket_recalled"
    # Dérogation au délai de grâce : un superviseur (ou au-dessus) a déclaré
    # absent un client encore dans sa fenêtre de grâce. C'est un acte
    # d'autorité — il raccourcit un délai imposé à l'agent — donc il se journalise
    # avec le temps restant, pas seulement dans l'historique du ticket.
    TICKET_ABSENT_OVERRIDE = "ticket_absent_override"
    # Connexions (constat P3-17) — « qui s'est connecté, quand, depuis où ».
    LOGIN_SUCCEEDED = "login_succeeded"
    LOGIN_FAILED = "login_failed"
    LOGIN_BLOCKED = "login_blocked"
    PASSWORD_CHANGED = "password_changed"
    PASSWORD_RESET_REQUESTED = "password_reset_requested"
    PASSWORD_RESET = "password_reset"


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # Nullable depuis P3-17 : un `super_admin` et un client OTP n'ont aucune
    # organisation. Sans cela, leurs connexions — les deux comptes les plus
    # sensibles — ne pouvaient pas être journalisées du tout.
    org_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    actor_role: Mapped[str | None] = mapped_column(String(30), nullable=True)
    action: Mapped[str] = mapped_column(String(50), index=True)
    target_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    extra_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} org={self.org_id} actor={self.actor_id}>"
