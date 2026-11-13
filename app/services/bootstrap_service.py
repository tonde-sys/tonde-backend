"""
Bootstrap idempotent d'une organisation pilote TONDE.

Ce module sert à préparer une installation réelle : première organisation,
première agence, services, guichets, administrateur d'organisation et comptes
initiaux. Il est volontairement séparé de l'API (aucun endpoint public) et
s'exécute en ligne de commande, après `alembic upgrade head`.

Principes :
  - idempotent : relancer la commande ne crée aucun doublon ;
  - non destructif : un mot de passe existant n'est jamais écrasé sans
    `reset_passwords=True` ;
  - transactionnel : toute erreur annule l'ensemble des écritures ;
  - utilisable en dry-run : les écritures sont annulées à la fin.

Exemple minimal :

    python -m app.cli bootstrap --config bootstrap/pilot.json

Le format du fichier est décrit dans `bootstrap/pilot.example.json` et dans
`DEPLOYMENT.md`.
"""
from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.branch import Branch, Service
from app.models.counter import Counter
from app.models.employee import Employee, EmployeeStatus
from app.models.organization import Organization
from app.models.user import User, UserRole
from app.models.user_organization import UserOrganization

VALID_SECTORS = {"bank", "hospital", "university", "administration", "other"}
VALID_STAFF_ROLES = {
    UserRole.SUPER_ADMIN,
    UserRole.ADMIN_ORG,
    UserRole.ADMIN_AGENCY,
    UserRole.SUPERVISOR,
    UserRole.AGENT,
}
ROLE_RANK = {
    UserRole.CLIENT: 0,
    UserRole.AGENT: 1,
    UserRole.SUPERVISOR: 2,
    UserRole.ADMIN_AGENCY: 3,
    UserRole.ADMIN_ORG: 4,
    UserRole.SUPER_ADMIN: 5,
}
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PREFIX_RE = re.compile(r"^[A-Z0-9]{1,5}$")


class BootstrapError(ValueError):
    """Configuration ou état incompatible avec le bootstrap demandé."""


@dataclass
class BootstrapResult:
    """Résumé sérialisable de ce qui a été créé ou vérifié."""

    organization: dict[str, Any]
    branch: dict[str, Any]
    services: list[dict[str, Any]] = field(default_factory=list)
    counters: list[dict[str, Any]] = field(default_factory=list)
    users: list[dict[str, Any]] = field(default_factory=list)
    generated_passwords: dict[str, str] = field(default_factory=dict)
    created: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "organization": self.organization,
            "branch": self.branch,
            "services": self.services,
            "counters": self.counters,
            "users": self.users,
            "generated_passwords": self.generated_passwords,
            "created": self.created,
        }


def _fail(message: str) -> None:
    raise BootstrapError(message)


def _text(
    value: Any,
    label: str,
    *,
    required: bool = True,
    max_length: int = 255,
) -> str | None:
    if value is None:
        if required:
            _fail(f"{label} est obligatoire")
        return None
    text = str(value).strip()
    if required and not text:
        _fail(f"{label} est obligatoire")
    if len(text) > max_length:
        _fail(f"{label} ne peut pas dépasser {max_length} caractères")
    return text or None


def _slug(value: Any, label: str) -> str:
    slug = _text(value, label, max_length=200)
    assert slug is not None
    if not _SLUG_RE.match(slug):
        _fail(
            f"{label} invalide : utilisez uniquement des minuscules, chiffres "
            "et tirets (ex. 'banco-bu-centre')"
        )
    return slug


def _email(value: Any, label: str = "email") -> str:
    email = _text(value, label, max_length=255)
    assert email is not None
    email = email.lower()
    if not _EMAIL_RE.match(email):
        _fail(f"{label} invalide : {email}")
    return email


def _positive_int(value: Any, label: str, default: int, *, maximum: int = 100_000) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        _fail(f"{label} doit être un entier")
    if number < 1 or number > maximum:
        _fail(f"{label} doit être compris entre 1 et {maximum}")
    return number


def _parse_role(value: Any, label: str) -> UserRole:
    raw = _text(value, label)
    assert raw is not None
    try:
        role = UserRole(raw.lower())
    except ValueError:
        _fail(f"{label} invalide : {raw}")
    if role not in VALID_STAFF_ROLES:
        _fail(f"{label} doit être un rôle staff (pas CLIENT)")
    return role


def _generate_password() -> str:
    # ~16 caractères URL-safe, largement au-dessus du minimum de 8.
    return secrets.token_urlsafe(12)


def _resolve_password(
    user_cfg: Mapping[str, Any],
    email: str,
    passwords: Mapping[str, str],
) -> str | None:
    """
    Priorité : mot de passe explicite du fichier, variable d'environnement,
    puis mapping `BOOTSTRAP_PASSWORDS_JSON`. Un mot de passe fourni doit faire
    au moins 8 caractères.
    """
    provided: Any = user_cfg.get("password")
    if not provided:
        env_name = user_cfg.get("password_env")
        if env_name:
            provided = os.getenv(str(env_name))
    if not provided:
        provided = passwords.get(email)

    if provided is None or str(provided) == "":
        return None
    password = str(provided)
    if len(password) < 8:
        _fail(f"Le mot de passe de {email} doit contenir au moins 8 caractères")
    return password


async def _get_or_create_organization(
    db: AsyncSession,
    cfg: Mapping[str, Any],
    created: dict[str, int],
) -> Organization:
    name = _text(cfg.get("name"), "organization.name", max_length=200)
    slug = _slug(cfg.get("slug"), "organization.slug")
    sector = _text(cfg.get("sector"), "organization.sector", max_length=50)
    assert name and sector
    sector = sector.lower()
    if sector not in VALID_SECTORS:
        _fail(
            "organization.sector invalide : valeurs acceptées "
            + ", ".join(sorted(VALID_SECTORS))
        )

    org = await db.scalar(select(Organization).where(Organization.slug == slug))
    if org is None:
        org = Organization(
            name=name,
            slug=slug,
            sector=sector,
            description=_text(cfg.get("description"), "organization.description", required=False, max_length=2000),
            country=_text(cfg.get("country"), "organization.country", required=False, max_length=50) or "Burundi",
            city=_text(cfg.get("city"), "organization.city", required=False, max_length=100),
            phone=_text(cfg.get("phone"), "organization.phone", required=False, max_length=20),
            email=_text(cfg.get("email"), "organization.email", required=False, max_length=255),
            website=_text(cfg.get("website"), "organization.website", required=False, max_length=500),
            logo_url=_text(cfg.get("logo_url"), "organization.logo_url", required=False, max_length=500),
            is_active=True,
        )
        db.add(org)
        await db.flush()
        created["organizations"] = created.get("organizations", 0) + 1
    else:
        # On complète uniquement les champs vides : un bootstrap ne doit pas
        # écraser silencieusement une fiche modifiée dans l'interface.
        for attr, value in {
            "description": _text(cfg.get("description"), "organization.description", required=False, max_length=2000),
            "country": _text(cfg.get("country"), "organization.country", required=False, max_length=50),
            "city": _text(cfg.get("city"), "organization.city", required=False, max_length=100),
            "phone": _text(cfg.get("phone"), "organization.phone", required=False, max_length=20),
            "email": _text(cfg.get("email"), "organization.email", required=False, max_length=255),
            "website": _text(cfg.get("website"), "organization.website", required=False, max_length=500),
            "logo_url": _text(cfg.get("logo_url"), "organization.logo_url", required=False, max_length=500),
        }.items():
            if value and not getattr(org, attr):
                setattr(org, attr, value)
    return org


async def _get_or_create_branch(
    db: AsyncSession,
    org: Organization,
    cfg: Mapping[str, Any],
    created: dict[str, int],
) -> Branch:
    name = _text(cfg.get("name"), "branch.name", max_length=200)
    slug = _slug(cfg.get("slug"), "branch.slug")
    assert name

    branch = await db.scalar(select(Branch).where(Branch.slug == slug))
    if branch is not None and branch.org_id != org.id:
        _fail(
            f"Le slug d'agence '{slug}' est déjà utilisé par une autre organisation"
        )

    if branch is None:
        branch = Branch(
            org_id=org.id,
            name=name,
            slug=slug,
            description=_text(cfg.get("description"), "branch.description", required=False, max_length=2000),
            sector=org.sector,
            phone=_text(cfg.get("phone"), "branch.phone", required=False, max_length=20),
            email=_text(cfg.get("email"), "branch.email", required=False, max_length=255),
            address=_text(cfg.get("address"), "branch.address", required=False, max_length=500),
            city=_text(cfg.get("city"), "branch.city", required=False, max_length=100) or org.city or "Bujumbura",
            country=_text(cfg.get("country"), "branch.country", required=False, max_length=50) or org.country,
            latitude=cfg.get("latitude"),
            longitude=cfg.get("longitude"),
            opens_at=_text(cfg.get("opens_at"), "branch.opens_at", required=False, max_length=5) or "08:00",
            closes_at=_text(cfg.get("closes_at"), "branch.closes_at", required=False, max_length=5) or "17:00",
            max_daily_tickets=_positive_int(cfg.get("max_daily_tickets"), "branch.max_daily_tickets", 500),
            avg_service_minutes=_positive_int(cfg.get("avg_service_minutes"), "branch.avg_service_minutes", 5),
            max_wait_minutes_alert=_positive_int(cfg.get("max_wait_minutes_alert"), "branch.max_wait_minutes_alert", 30),
            enable_sms_reminders=bool(cfg.get("enable_sms_reminders", False)),
            is_active=True,
            is_open=bool(cfg.get("is_open", True)),
        )
        db.add(branch)
        await db.flush()
        created["branches"] = created.get("branches", 0) + 1
    else:
        if not branch.is_active:
            branch.is_active = True
        if cfg.get("is_open") is True:
            branch.is_open = True
        for attr, value in {
            "description": _text(cfg.get("description"), "branch.description", required=False, max_length=2000),
            "phone": _text(cfg.get("phone"), "branch.phone", required=False, max_length=20),
            "email": _text(cfg.get("email"), "branch.email", required=False, max_length=255),
            "address": _text(cfg.get("address"), "branch.address", required=False, max_length=500),
            "city": _text(cfg.get("city"), "branch.city", required=False, max_length=100),
        }.items():
            if value and not getattr(branch, attr):
                setattr(branch, attr, value)
    return branch


async def _ensure_services(
    db: AsyncSession,
    org: Organization,
    branch: Branch,
    services_cfg: list[Mapping[str, Any]],
    created: dict[str, int],
) -> list[Service]:
    if not services_cfg:
        _fail("services doit contenir au moins un service")

    seen_prefixes: set[str] = set()
    seen_names: set[str] = set()
    services: list[Service] = []

    for index, cfg in enumerate(services_cfg):
        name = _text(cfg.get("name"), f"services[{index}].name", max_length=200)
        assert name
        if name in seen_names:
            _fail(f"Le service '{name}' est présent plusieurs fois")
        seen_names.add(name)

        prefix = _text(cfg.get("ticket_prefix"), f"services[{index}].ticket_prefix", required=False, max_length=5)
        prefix = (prefix or chr(65 + index % 26)).upper()
        if not _PREFIX_RE.match(prefix):
            _fail(
                f"services[{index}].ticket_prefix invalide : 1 à 5 caractères "
                "majuscules ou chiffres (ex. 'A', 'CAISSE')"
            )
        if prefix in seen_prefixes:
            _fail(f"Le préfixe de ticket '{prefix}' est utilisé plusieurs fois")
        seen_prefixes.add(prefix)

        service = await db.scalar(
            select(Service).where(Service.branch_id == branch.id, Service.name == name)
        )
        if service is None:
            conflict = await db.scalar(
                select(Service).where(
                    Service.branch_id == branch.id,
                    Service.ticket_prefix == prefix,
                    Service.name != name,
                )
            )
            if conflict is not None:
                _fail(
                    f"Le préfixe '{prefix}' est déjà utilisé par le service "
                    f"'{conflict.name}' de cette agence"
                )
            service = Service(
                org_id=org.id,
                branch_id=branch.id,
                name=name,
                description=_text(cfg.get("description"), f"services[{index}].description", required=False, max_length=2000),
                ticket_prefix=prefix,
                avg_duration_minutes=_positive_int(
                    cfg.get("avg_duration_minutes"),
                    f"services[{index}].avg_duration_minutes",
                    5,
                ),
                is_active=True,
            )
            db.add(service)
            await db.flush()
            created["services"] = created.get("services", 0) + 1
        else:
            if not service.is_active:
                service.is_active = True
            if not service.org_id:
                service.org_id = org.id
        services.append(service)

    return services


async def _ensure_counters(
    db: AsyncSession,
    org: Organization,
    branch: Branch,
    counters_cfg: list[Mapping[str, Any]],
    created: dict[str, int],
) -> list[Counter]:
    if not counters_cfg:
        _fail("counters doit contenir au moins un guichet")

    counters: list[Counter] = []
    seen_names: set[str] = set()
    for index, cfg in enumerate(counters_cfg):
        name = _text(cfg.get("name"), f"counters[{index}].name", max_length=100)
        assert name
        if name in seen_names:
            _fail(f"Le guichet '{name}' est présent plusieurs fois")
        seen_names.add(name)

        counter = await db.scalar(
            select(Counter).where(Counter.agency_id == branch.id, Counter.name == name)
        )
        if counter is None:
            counter = Counter(
                org_id=org.id,
                agency_id=branch.id,
                name=name,
                description=_text(cfg.get("description"), f"counters[{index}].description", required=False, max_length=2000),
                is_open=bool(cfg.get("is_open", True)),
                is_active=True,
            )
            db.add(counter)
            await db.flush()
            created["counters"] = created.get("counters", 0) + 1
        else:
            if not counter.is_active:
                counter.is_active = True
        counters.append(counter)

    return counters


async def _ensure_user(
    db: AsyncSession,
    org: Organization,
    branch: Branch,
    user_cfg: Mapping[str, Any],
    counters_by_name: Mapping[str, Counter],
    passwords: Mapping[str, str],
    *,
    reset_passwords: bool,
    created: dict[str, int],
    result: BootstrapResult,
) -> User:
    name = _text(user_cfg.get("name"), "users[].name", max_length=100)
    email = _email(user_cfg.get("email"), "users[].email")
    requested_role = _parse_role(user_cfg.get("role"), f"users[{email}].role")
    assert name

    user = await db.scalar(select(User).where(User.email == email))
    is_new = user is None

    if is_new:
        user = User(
            name=name,
            email=email,
            role=requested_role,
            org_id=None if requested_role == UserRole.SUPER_ADMIN else org.id,
            is_active=True,
            is_verified=True,
            language="fr",
        )
        db.add(user)
        await db.flush()
        created["users"] = created.get("users", 0) + 1
    else:
        # Un utilisateur déjà rattaché à une autre organisation ne doit pas
        # être capturé silencieusement. Les SUPER_ADMIN sont globaux.
        if (
            user.org_id
            and user.org_id != org.id
            and user.role != UserRole.SUPER_ADMIN
            and requested_role != UserRole.SUPER_ADMIN
        ):
            _fail(
                f"L'utilisateur {email} appartient déjà à une autre organisation "
                f"(org_id={user.org_id})"
            )
        if not user.is_active:
            user.is_active = True
        if not user.is_verified:
            user.is_verified = True
        if not user.name:
            user.name = name

    # Ne jamais rétrograder un SUPER_ADMIN existant, ni un membre du staff
    # existant, par accident lors d'un simple rerun du bootstrap.
    effective_role = requested_role
    if user.role == UserRole.SUPER_ADMIN and requested_role != UserRole.SUPER_ADMIN:
        effective_role = UserRole.SUPER_ADMIN
    elif ROLE_RANK[user.role] > ROLE_RANK[requested_role] and user.role != UserRole.CLIENT:
        effective_role = user.role
    elif ROLE_RANK[requested_role] > ROLE_RANK[user.role]:
        effective_role = requested_role

    user.role = effective_role
    user.org_id = None if effective_role == UserRole.SUPER_ADMIN else org.id

    # Mot de passe : uniquement si nécessaire, ou sur demande explicite.
    needs_password = reset_passwords or is_new or not user.hashed_password
    provided_password = _resolve_password(user_cfg, email, passwords)
    if needs_password:
        password = provided_password or _generate_password()
        if provided_password is None:
            result.generated_passwords[email] = password
        user.hashed_password = hash_password(password)

    result.users.append(
        {
            "id": user.id,
            "email": email,
            "name": name,
            "role": effective_role.value,
            "created": is_new,
            "password_generated": email in result.generated_passwords,
        }
    )

    if effective_role == UserRole.SUPER_ADMIN:
        return user

    # Table pivot multi-organisations.
    membership = await db.scalar(
        select(UserOrganization).where(
            UserOrganization.user_id == user.id,
            UserOrganization.organization_id == org.id,
        )
    )
    if membership is None:
        db.add(
            UserOrganization(
                user_id=user.id,
                organization_id=org.id,
                status="active",
            )
        )
        created["memberships"] = created.get("memberships", 0) + 1
    elif membership.status != "active":
        membership.status = "active"

    # Fiche employé : rôle opérationnel, agence et guichet.
    counter_name = _text(
        user_cfg.get("counter"),
        f"users[{email}].counter",
        required=False,
        max_length=100,
    )
    counter = None
    if counter_name:
        counter = counters_by_name.get(counter_name)
        if counter is None:
            _fail(
                f"Le guichet '{counter_name}' demandé pour {email} n'existe pas "
                "dans cette agence"
            )

    employee = await db.scalar(
        select(Employee).where(Employee.user_id == user.id, Employee.org_id == org.id)
    )
    if employee is None:
        db.add(
            Employee(
                org_id=org.id,
                user_id=user.id,
                agency_id=branch.id,
                counter_id=counter.id if counter else None,
                role=effective_role,
                status=EmployeeStatus.ACTIVE,
            )
        )
        created["employees"] = created.get("employees", 0) + 1
    else:
        employee.agency_id = branch.id
        employee.counter_id = counter.id if counter else None
        employee.role = effective_role
        employee.status = EmployeeStatus.ACTIVE

    return user


async def bootstrap_pilot(
    db: AsyncSession,
    config: Mapping[str, Any],
    *,
    passwords: Mapping[str, str] | None = None,
    reset_passwords: bool = False,
    dry_run: bool = False,
) -> BootstrapResult:
    """
    Crée (ou vérifie) une organisation pilote complète.

    Args:
        db: session SQLAlchemy async.
        config: structure décrite dans `bootstrap/pilot.example.json`.
        passwords: mapping email -> mot de passe (ex. BOOTSTRAP_PASSWORDS_JSON).
        reset_passwords: réinitialise les mots de passe existants.
        dry_run: exécute les requêtes puis annule la transaction.

    Returns:
        BootstrapResult avec les identifiants et les mots de passe générés.

    Raises:
        BootstrapError: configuration invalide ou conflit avec l'existant.
    """
    passwords = passwords or {}
    created: dict[str, int] = {}
    result: BootstrapResult | None = None

    try:
        org_cfg = config.get("organization")
        branch_cfg = config.get("branch")
        services_cfg = config.get("services") or []
        counters_cfg = config.get("counters") or []
        users_cfg = config.get("users") or []

        if not isinstance(org_cfg, Mapping):
            _fail("organization est obligatoire")
        if not isinstance(branch_cfg, Mapping):
            _fail("branch est obligatoire")
        if not isinstance(services_cfg, list) or not services_cfg:
            _fail("services doit être une liste non vide")
        if not isinstance(counters_cfg, list) or not counters_cfg:
            _fail("counters doit être une liste non vide")
        if not isinstance(users_cfg, list) or not users_cfg:
            _fail("users doit être une liste non vide")

        roles = [
            _parse_role(user.get("role"), "users[].role")
            for user in users_cfg
            if isinstance(user, Mapping)
        ]
        if UserRole.ADMIN_ORG not in roles and UserRole.SUPER_ADMIN not in roles:
            _fail("users doit contenir au moins un admin_org ou un super_admin")

        org = await _get_or_create_organization(db, org_cfg, created)
        branch = await _get_or_create_branch(db, org, branch_cfg, created)
        services = await _ensure_services(db, org, branch, services_cfg, created)
        counters = await _ensure_counters(db, org, branch, counters_cfg, created)
        counters_by_name = {counter.name: counter for counter in counters}

        result = BootstrapResult(
            organization={"id": org.id, "name": org.name, "slug": org.slug},
            branch={"id": branch.id, "name": branch.name, "slug": branch.slug},
            services=[
                {
                    "id": service.id,
                    "name": service.name,
                    "ticket_prefix": service.ticket_prefix,
                }
                for service in services
            ],
            counters=[
                {"id": counter.id, "name": counter.name} for counter in counters
            ],
            created=created,
            dry_run=dry_run,
        )

        for user_cfg in users_cfg:
            if not isinstance(user_cfg, Mapping):
                _fail("Chaque entrée users doit être un objet")
            await _ensure_user(
                db,
                org,
                branch,
                user_cfg,
                counters_by_name,
                passwords,
                reset_passwords=reset_passwords,
                created=created,
                result=result,
            )

        if dry_run:
            await db.rollback()
        else:
            await db.commit()
        return result
    except Exception:
        await db.rollback()
        raise
