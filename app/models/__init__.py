"""
Import explicite de tous les modèles SQLAlchemy.

Ce fichier garantit que Base.metadata connaît toutes les tables,
que ce soit pour Alembic (migrations) ou create_tables() en dev.

Règle : ajouter ici tout nouveau modèle créé dans ce dossier.
Ordre d'import : respecter les dépendances FK (parent avant enfant).
"""
from app.models.organization import Organization        # noqa: F401
from app.models.user import User, UserRole              # noqa: F401
from app.models.branch import Branch, Service           # noqa: F401  Sprint 2 : Agency → Branch
from app.models.counter import Counter                  # noqa: F401
from app.models.employee import Employee, EmployeeStatus  # noqa: F401
from app.models.ticket import (                         # noqa: F401
    Ticket,
    TicketStatus,
    TicketPriority,
    PRIORITY_SCORES,
    ALLOWED_TRANSITIONS,
)
from app.models.refresh_token import RefreshToken       # noqa: F401
from app.models.user_organization import UserOrganization  # noqa: F401  Sprint 2 : S2-02
from app.models.notification import (              # noqa: F401
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.queue_log import QueueLog, QueueLogAction  # noqa: F401
from app.models.audit_log import AuditLog, AuditAction      # noqa: F401
