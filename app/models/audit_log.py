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


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), index=True
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
