"""
Router Audit — consultation du journal des actions sensibles.

  GET /api/v1/audit/logs   connexions et actions sensibles, filtrables

Pourquoi une route de LECTURE : le journal existait depuis le début, mais rien
ne permettait de l'interroger. « Qui s'est connecté hier entre 22 h et 6 h ? »
n'avait pas de réponse sans accès SQL direct à la production — ce qu'une banque
n'accordera pas. Le constat P3-17 ne se referme pas en écrivant des lignes, il
se referme en permettant de les lire.

Périmètre :
  - ADMIN_ORG et au-dessus : les lignes de LEUR organisation ;
  - SUPER_ADMIN : toutes, y compris celles sans organisation (super-admins et
    clients OTP, qui n'appartiennent à aucune organisation).
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_admin_org
from app.models.user import User, UserRole
from app.services.audit_service import AuditService

router = APIRouter()


@router.get("/logs", summary="Journal d'audit (connexions, exports, priorités…)")
async def list_audit_logs(
    action: str | None = Query(default=None, description="Filtrer sur un type d'action"),
    actor_id: str | None = Query(default=None, description="Filtrer sur un utilisateur"),
    org_id: str | None = Query(
        default=None,
        description="Super-admin uniquement : consulter une autre organisation",
    ),
    date_from: datetime | None = Query(default=None, description="Début de période (ISO 8601)"),
    date_to: datetime | None = Query(default=None, description="Fin de période (ISO 8601)"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(get_current_admin_org),
    db: AsyncSession = Depends(get_db),
):
    """
    Journal d'audit, du plus récent au plus ancien.

    - Un `admin_org` ne voit que son organisation, quoi qu'il demande : le
      paramètre `org_id` est ignoré pour lui (pas de fuite inter-tenant).
    - Un `super_admin` voit tout, et peut cibler une organisation.
    - `action` accepte les valeurs de `AuditAction` (`login_succeeded`,
      `login_failed`, `login_blocked`, `ticket_exported`, …).
    """
    service = AuditService(db)

    # SUPER_ADMIN n'a pas d'org_id : `None` signifie « toutes les organisations »,
    # et le paramètre `org_id` permet d'en cibler une.
    # Tout autre rôle autorisé ici est ADMIN_ORG : borné à son organisation, sans
    # exception — le paramètre `org_id` est alors ignoré, jamais honoré.
    target_org = org_id if current_user.role == UserRole.SUPER_ADMIN else current_user.org_id

    rows, total = await service.search(
        org_id=target_org,
        action=action,
        actor_id=actor_id,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )

    return {
        "items": [
            {
                "id": row.id,
                "org_id": row.org_id,
                "actor_id": row.actor_id,
                "actor_role": row.actor_role,
                "action": row.action,
                "target_type": row.target_type,
                "target_id": row.target_id,
                "extra": row.extra_data,
                "created_at": row.created_at,
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_next": (page * page_size) < total,
    }
