"""
Contrôle d'accès multi-tenant et par branche — source unique de vérité.

Trois niveaux de périmètre :

  ORG_WIDE     ADMIN_ORG, SUPER_ADMIN → toute l'organisation (SUPER_ADMIN : toutes les orgs)
  BRANCH       AGENT, SUPERVISOR, ADMIN_AGENCY → uniquement les branches où ils sont
               affectés (Employee actif, via agency_id ou via leur guichet)
  CLIENT       aucun accès staff

Avant ce module, un SUPERVISOR de la branche A pouvait consulter les statistiques
de la branche B de la même organisation : seule l'organisation était vérifiée.
"""
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.branch import Branch
from app.models.counter import Counter
from app.models.employee import Employee, EmployeeStatus
from app.models.user import User, UserRole

ORG_WIDE_ROLES: frozenset[UserRole] = frozenset({UserRole.ADMIN_ORG, UserRole.SUPER_ADMIN})

# Hiérarchie croissante — utilisée pour empêcher l'escalade de privilèges
ROLE_RANK: dict[UserRole, int] = {
    UserRole.CLIENT: 0,
    UserRole.AGENT: 1,
    UserRole.SUPERVISOR: 2,
    UserRole.ADMIN_AGENCY: 3,
    UserRole.ADMIN_ORG: 4,
    UserRole.SUPER_ADMIN: 5,
}


def forbidden(message: str, code: str = "FORBIDDEN") -> HTTPException:
    """Construit l'erreur 403 standard."""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": code, "message": message},
    )


def assert_org_access(user: User, org_id: str) -> None:
    """
    Vérifie que l'utilisateur peut agir sur l'organisation cible.

    SUPER_ADMIN : toutes les organisations. Les autres : la leur uniquement.

    Raises:
        HTTPException 403: Organisation différente de celle de l'utilisateur
    """
    if user.role == UserRole.SUPER_ADMIN:
        return
    if not user.org_id or user.org_id != org_id:
        raise forbidden("Accès refusé à cette organisation")


async def get_staff_branch_ids(db: AsyncSession, user: User) -> set[str]:
    """
    Retourne les branches auxquelles un employé est affecté.

    Une branche compte si l'employé (actif) y est rattaché directement
    (Employee.agency_id) OU via son guichet (Counter.agency_id).
    """
    result = await db.execute(
        select(Employee.agency_id, Counter.agency_id)
        .outerjoin(Counter, Counter.id == Employee.counter_id)
        .where(
            Employee.user_id == user.id,
            Employee.status == EmployeeStatus.ACTIVE,
        )
    )
    branch_ids: set[str] = set()
    for employee_branch, counter_branch in result.all():
        if employee_branch:
            branch_ids.add(employee_branch)
        if counter_branch:
            branch_ids.add(counter_branch)
    return branch_ids


async def assert_branch_access(
    db: AsyncSession, user: User, org_id: str, branch_id: str
) -> Branch:
    """
    Vérifie que l'utilisateur peut agir sur une branch donnée.

    - La branch doit exister dans l'organisation (sinon 404, sans révéler d'autre org).
    - ADMIN_ORG / SUPER_ADMIN : toutes les branches de l'organisation.
    - AGENT / SUPERVISOR / ADMIN_AGENCY : uniquement leurs branches d'affectation.

    Returns:
        La Branch (évite une seconde requête à l'appelant).

    Raises:
        HTTPException 403: Pas d'accès à cette organisation ou à cette branch
        HTTPException 404: Branch introuvable dans cette organisation
    """
    assert_org_access(user, org_id)

    result = await db.execute(
        select(Branch).where(Branch.id == branch_id, Branch.org_id == org_id)
    )
    branch = result.scalar_one_or_none()
    if branch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "BRANCH_NOT_FOUND", "message": "Branch introuvable"},
        )

    if user.role in ORG_WIDE_ROLES:
        return branch

    if branch_id not in await get_staff_branch_ids(db, user):
        raise forbidden("Vous n'êtes pas affecté à cette agence", code="BRANCH_FORBIDDEN")
    return branch


def assert_can_assign_role(actor: User, target_role: UserRole) -> None:
    """
    Empêche l'escalade de privilèges lors de la création/modification d'un employé.

    Règles :
      - SUPER_ADMIN peut attribuer n'importe quel rôle.
      - Personne d'autre ne peut attribuer SUPER_ADMIN.
      - ADMIN_ORG peut attribuer jusqu'à ADMIN_ORG (pas SUPER_ADMIN).
      - ADMIN_AGENCY ne peut attribuer que des rôles STRICTEMENT inférieurs au sien
        (AGENT, SUPERVISOR) : il ne peut pas se créer un pair ni un supérieur.

    Raises:
        HTTPException 403: Rôle supérieur ou égal (sauf ADMIN_ORG) à celui de l'acteur
    """
    if actor.role == UserRole.SUPER_ADMIN:
        return
    if target_role == UserRole.SUPER_ADMIN:
        raise forbidden("Seul un super-admin peut attribuer ce rôle", code="ROLE_ESCALATION")
    if actor.role == UserRole.ADMIN_ORG:
        return
    if ROLE_RANK[target_role] >= ROLE_RANK[actor.role]:
        raise forbidden(
            f"Vous ne pouvez pas attribuer le rôle {target_role.value}",
            code="ROLE_ESCALATION",
        )
