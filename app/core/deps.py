"""
Dépendances FastAPI — injectées dans les routes protégées.

Hiérarchie RBAC :
  CLIENT < AGENT < SUPERVISOR < ADMIN_AGENCY < ADMIN_ORG < SUPER_ADMIN

Chaque dépendance vérifie :
  1. JWT valide et non expiré
  2. Utilisateur existant et actif en base
  3. Rôle suffisant pour l'action demandée
"""
import logging
from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.core.security import verify_token
from app.models.user import User, UserRole

logger = logging.getLogger(__name__)

# Schéma Bearer Token pour Swagger + clients HTTP
bearer_scheme = HTTPBearer()

# Hiérarchie des rôles — ordre croissant de permissions
ROLE_HIERARCHY = [
    UserRole.CLIENT,
    UserRole.AGENT,
    UserRole.SUPERVISOR,
    UserRole.ADMIN_AGENCY,
    UserRole.ADMIN_ORG,
    UserRole.SUPER_ADMIN,
]


async def _load_user(user_id: str, db: AsyncSession) -> User:
    """
    Charge un utilisateur depuis la DB et vérifie qu'il est actif.

    Raises:
        HTTPException 401: Utilisateur introuvable ou inactif
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "USER_NOT_FOUND", "message": "Utilisateur introuvable ou désactivé"},
        )
    return user


def _require_role(minimum_role: UserRole):
    """
    Factory qui retourne une dépendance vérifiant le rôle minimum.

    Utilisation :
        @router.post("/")
        async def endpoint(user = Depends(_require_role(UserRole.AGENT))):
    """
    async def checker(user: User = Depends(get_current_user)) -> User:
        if ROLE_HIERARCHY.index(user.role) < ROLE_HIERARCHY.index(minimum_role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "FORBIDDEN",
                    "message": f"Rôle requis : {minimum_role.value}. Rôle actuel : {user.role.value}",
                },
            )
        return user
    return checker


# ── Dépendance principale ─────────────────────────────────────────────────────
async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Extrait et vérifie le JWT de chaque requête protégée.

    Utilisation :
        @router.get("/me")
        async def get_me(current_user: User = Depends(get_current_user)):
            return current_user

    Raises:
        HTTPException 401: Token invalide, expiré ou utilisateur introuvable
    """
    token = credentials.credentials
    user_id = verify_token(token, token_type="access")

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": "Token invalide ou expiré"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    return await _load_user(user_id, db)


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False)),
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """
    Retourne l'utilisateur connecté s'il y en a un, sinon None (route publique).

    Un jeton ABSENT donne None. Un jeton PRÉSENT mais invalide est refusé (401) : on
    ne dégrade pas silencieusement une requête authentifiée en requête anonyme.

    Raises:
        HTTPException 401: Jeton fourni mais invalide / utilisateur désactivé
    """
    if credentials is None:
        return None
    user_id = verify_token(credentials.credentials, token_type="access")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": "Token invalide ou expiré"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await _load_user(user_id, db)


# ── Dépendances par rôle ──────────────────────────────────────────────────────
async def get_current_agent(
    current_user: User = Depends(_require_role(UserRole.AGENT)),
) -> User:
    """Vérifie rôle ≥ AGENT (agent, supervisor, admin_agency, admin_org, super_admin)."""
    return current_user


async def get_current_supervisor(
    current_user: User = Depends(_require_role(UserRole.SUPERVISOR)),
) -> User:
    """Vérifie rôle ≥ SUPERVISOR."""
    return current_user


async def get_current_admin_agency(
    current_user: User = Depends(_require_role(UserRole.ADMIN_AGENCY)),
) -> User:
    """Vérifie rôle ≥ ADMIN_AGENCY."""
    return current_user


async def get_current_admin_org(
    current_user: User = Depends(_require_role(UserRole.ADMIN_ORG)),
) -> User:
    """Vérifie rôle ≥ ADMIN_ORG."""
    return current_user


async def get_current_super_admin(
    current_user: User = Depends(_require_role(UserRole.SUPER_ADMIN)),
) -> User:
    """Vérifie rôle = SUPER_ADMIN uniquement (équipe Tonde)."""
    return current_user


# ── Auth WebSocket ────────────────────────────────────────────────────────────
async def get_ws_user(
    token: str = Query(..., description="JWT access token"),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Dépendance pour authentifier une connexion WebSocket.
    Le JWT est passé en query param car les headers ne sont
    pas facilement accessibles dans WebSocket.

    Exemple d'URL :
        ws://api.tonde.app/api/v1/tickets/ws/queue/<ticket_id>
        ?token=<jwt>&agency_id=<agency_id>

    Raises:
        HTTPException 401: Token invalide ou manquant
    """
    user_id = verify_token(token, token_type="access")

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": "Token WebSocket invalide ou expiré"},
        )

    return await _load_user(user_id, db)
