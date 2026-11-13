"""
Commandes d'exploitation TONDE.

Usage :
    python -m app.cli bootstrap --config bootstrap/pilot.json
    python -m app.cli bootstrap --config bootstrap/pilot.json --dry-run
    python -m app.cli bootstrap --org-name "..." --org-slug ... \
        --branch-name "..." --branch-slug ... \
        --admin-email ... --super-admin-email ... \
        --service "Caisse|A|5" --counter "Guichet 1"

Les mots de passe peuvent être fournis par :
  - le champ `password` du fichier (déconseillé) ;
  - `password_env` (nom d'une variable d'environnement) ;
  - la variable `BOOTSTRAP_PASSWORDS_JSON` : {"email": "mot-de-passe"}.

À défaut, un mot de passe aléatoire est généré et affiché UNE SEULE FOIS.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from app.core.database import AsyncSessionLocal, engine
from app.services.bootstrap_service import (
    BootstrapError,
    BootstrapResult,
    bootstrap_pilot,
)


def _load_config(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    config_path = Path(path)
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BootstrapError(f"Fichier de configuration illisible : {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"JSON invalide dans {config_path} : {exc}") from exc
    if not isinstance(data, dict):
        raise BootstrapError("Le fichier de configuration doit contenir un objet JSON")
    return data


def _parse_service(value: str) -> dict[str, Any]:
    parts = [part.strip() for part in value.split("|")]
    if not parts or not parts[0]:
        raise BootstrapError("--service attend 'Nom' ou 'Nom|Préfixe|Minutes'")
    service: dict[str, Any] = {"name": parts[0]}
    if len(parts) > 1 and parts[1]:
        service["ticket_prefix"] = parts[1]
    if len(parts) > 2 and parts[2]:
        service["avg_duration_minutes"] = parts[2]
    if len(parts) > 3:
        service["description"] = "|".join(parts[3:])
    return service


def _parse_employee(value: str) -> dict[str, Any]:
    parts = [part.strip() for part in value.split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise BootstrapError("--employee attend 'Nom|email[|rôle|guichet]'")
    employee: dict[str, Any] = {
        "name": parts[0],
        "email": parts[1],
        "role": parts[2] if len(parts) > 2 and parts[2] else "agent",
    }
    if len(parts) > 3 and parts[3]:
        employee["counter"] = parts[3]
    return employee


def _config_from_args(args: argparse.Namespace) -> dict[str, Any]:
    config = _load_config(args.config)
    org = config.setdefault("organization", {})
    branch = config.setdefault("branch", {})
    users = config.setdefault("users", [])
    if not isinstance(org, dict) or not isinstance(branch, dict) or not isinstance(users, list):
        raise BootstrapError(
            "Le fichier doit contenir les objets organization, branch et la liste users"
        )

    for attr in ("name", "slug", "sector", "country", "city", "phone", "email"):
        value = getattr(args, f"org_{attr}", None)
        if value is not None:
            org[attr] = value

    for attr in ("name", "slug", "address", "city", "phone", "email", "opens_at", "closes_at"):
        value = getattr(args, f"branch_{attr}", None)
        if value is not None:
            branch[attr] = value

    if args.service:
        config["services"] = [_parse_service(value) for value in args.service]
    if args.counter:
        config["counters"] = [{"name": name} for name in args.counter]

    if args.super_admin_email:
        users.append(
            {
                "name": args.super_admin_name or "Super administrateur TONDE",
                "email": args.super_admin_email,
                "role": "super_admin",
            }
        )
    if args.admin_email:
        users.append(
            {
                "name": args.admin_name or "Administrateur de l'organisation",
                "email": args.admin_email,
                "role": "admin_org",
            }
        )
    for value in args.employee or []:
        users.append(_parse_employee(value))

    return config


def _load_passwords() -> dict[str, str]:
    raw = os.getenv("BOOTSTRAP_PASSWORDS_JSON", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"BOOTSTRAP_PASSWORDS_JSON invalide : {exc}") from exc
    if not isinstance(data, dict):
        raise BootstrapError("BOOTSTRAP_PASSWORDS_JSON doit être un objet JSON")
    return {str(key).lower(): str(value) for key, value in data.items()}


def _print_result(result: BootstrapResult) -> None:
    data = result.as_dict()
    print("=" * 62)
    print("TONDE — Bootstrap de l'organisation pilote")
    print("=" * 62)
    if result.dry_run:
        print("MODE DRY-RUN : aucune écriture n'a été conservée.")
        print("-" * 62)
    print(f"Organisation : {data['organization']['name']} ({data['organization']['slug']})")
    print(f"  id         : {data['organization']['id']}")
    print(f"Agence       : {data['branch']['name']} ({data['branch']['slug']})")
    print(f"  id         : {data['branch']['id']}")
    print("Services     : " + ", ".join(
        f"{item['name']} [{item['ticket_prefix']}]" for item in data["services"]
    ))
    print("Guichets     : " + ", ".join(item["name"] for item in data["counters"]))
    print("-" * 62)
    print("Utilisateurs :")
    for user in data["users"]:
        flags = []
        if user["created"]:
            flags.append("créé")
        if user["password_generated"]:
            flags.append("mot de passe généré")
        suffix = f" ({', '.join(flags)})" if flags else " (déjà présent)"
        print(f"  - {user['email']} — {user['role']}{suffix}")

    created = ", ".join(f"{key}={value}" for key, value in data["created"].items())
    if created:
        print("-" * 62)
        print("Créations    : " + created)

    if data["generated_passwords"]:
        print("=" * 62)
        print("MOTS DE PASSE GÉNÉRÉS — AFFICHEZ-LES MAINTENANT")
        print("Ils ne seront plus jamais affichés. Conservez-les dans un")
        print("gestionnaire de secrets et changez-les après la première connexion.")
        print("=" * 62)
        for email, password in data["generated_passwords"].items():
            print(f"  {email} : {password}")
        print("=" * 62)


async def _run_bootstrap(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    passwords = _load_passwords()
    async with AsyncSessionLocal() as db:
        try:
            result = await bootstrap_pilot(
                db,
                config,
                passwords=passwords,
                reset_passwords=args.reset_passwords,
                dry_run=args.dry_run,
            )
        finally:
            await engine.dispose()

    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    else:
        _print_result(result)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Commandes d'exploitation TONDE",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap = subparsers.add_parser(
        "bootstrap",
        help="Crée ou vérifie une organisation pilote (idempotent)",
    )
    bootstrap.add_argument("--config", help="Fichier JSON de configuration")
    bootstrap.add_argument("--org-name")
    bootstrap.add_argument("--org-slug")
    bootstrap.add_argument("--org-sector", choices=["bank", "hospital", "university", "administration", "other"])
    bootstrap.add_argument("--org-country")
    bootstrap.add_argument("--org-city")
    bootstrap.add_argument("--org-phone")
    bootstrap.add_argument("--org-email")
    bootstrap.add_argument("--branch-name")
    bootstrap.add_argument("--branch-slug")
    bootstrap.add_argument("--branch-address")
    bootstrap.add_argument("--branch-city")
    bootstrap.add_argument("--branch-phone")
    bootstrap.add_argument("--branch-email")
    bootstrap.add_argument("--branch-opens-at")
    bootstrap.add_argument("--branch-closes-at")
    bootstrap.add_argument(
        "--service",
        action="append",
        help="Service : 'Nom|Préfixe|Minutes' (répétable)",
    )
    bootstrap.add_argument(
        "--counter",
        action="append",
        help="Nom du guichet (répétable)",
    )
    bootstrap.add_argument("--super-admin-name")
    bootstrap.add_argument("--super-admin-email")
    bootstrap.add_argument("--admin-name")
    bootstrap.add_argument("--admin-email")
    bootstrap.add_argument(
        "--employee",
        action="append",
        help="Employé : 'Nom|email|rôle|guichet' (répétable)",
    )
    bootstrap.add_argument(
        "--reset-passwords",
        action="store_true",
        help="Réinitialise les mots de passe des comptes existants",
    )
    bootstrap.add_argument(
        "--dry-run",
        action="store_true",
        help="Valide et simule sans conserver les écritures",
    )
    bootstrap.add_argument("--json", action="store_true", help="Sortie JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "bootstrap":
            return asyncio.run(_run_bootstrap(args))
    except BootstrapError as exc:
        print(f"Erreur de bootstrap : {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - filet de sécurité CLI
        print(f"Erreur inattendue : {exc}", file=sys.stderr)
        if os.getenv("BOOTSTRAP_DEBUG", "").lower() in {"1", "true", "yes"}:
            raise
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
