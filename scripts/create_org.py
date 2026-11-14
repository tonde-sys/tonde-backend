#!/usr/bin/env python3
"""
Crée une organisation complète via l'API TONDE (aucun accès base requis).

L'API expose la création en 6 étapes séparées ; ce script les enchaîne dans le
bon ordre et s'arrête à la première erreur en indiquant l'étape fautive :

    1. login SUPER_ADMIN                      -> token
    2. POST /organizations                    -> org_id
    3. POST /organizations/{org}/branches     -> branch_id
    4. POST .../{branch}/services             -> n services
    5. POST .../{branch}/counters             -> n guichets
    6. register + POST /organizations/{org}/employees  -> premier admin_org

Exemples
--------
Création complète (les mots de passe peuvent venir de l'environnement) :

    export TONDE_SUPER_PASSWORD='...'
    python scripts/create_org.py \
        --org-name "Clinique Espoir" --org-slug clinique-espoir --sector hospital \
        --city Bujumbura --branch-name "Siège" --branch-slug siege \
        --service "Consultation|C|10" --service "Laboratoire|L|15" \
        --counter "Guichet 1" --counter "Guichet 2" \
        --admin-name "Alice N." --admin-email alice@espoir.bi --admin-password 'MotDePasse123'

Simulation sans écriture :

    python scripts/create_org.py --dry-run --org-name ... --org-slug ...

Sortie machine (CI) : ajouter --json.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_API = "https://tonde-api-pilot-fx1j.onrender.com"
SECTORS = ("bank", "hospital", "university", "administration", "other")


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, step: str) -> None:
        super().__init__(f"[{step}] HTTP {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.step = step


def _parse_detail(payload: object) -> tuple[str, str]:
    """Extrait (code, message) de l'enveloppe d'erreur TONDE ou de Pydantic."""
    if isinstance(payload, dict):
        detail = payload.get("detail", payload)
        if isinstance(detail, dict):
            code = str(detail.get("code", "ERROR"))
            message = str(detail.get("message", detail))
            return code, message
        if isinstance(detail, list):  # erreurs de validation Pydantic (422)
            parts = []
            for item in detail:
                if isinstance(item, dict):
                    loc = ".".join(str(x) for x in item.get("loc", []) if x != "body")
                    parts.append(f"{loc}: {item.get('msg', '')}".strip(": "))
            return "VALIDATION_ERROR", " | ".join(parts) or "Requête invalide"
        return "ERROR", str(detail)
    return "ERROR", str(payload)


class Client:
    def __init__(self, base_url: str, dry_run: bool = False, quiet: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run
        self.quiet = quiet
        self.token: str | None = None

    def _trace(self, message: str) -> None:
        """En mode --json, la trace part sur stderr pour ne pas casser le JSON."""
        print(message, file=sys.stderr if self.quiet else sys.stdout)

    def call(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        step: str,
        auth: bool = True,
        allow_status: tuple[int, ...] = (),
    ) -> dict:
        if self.dry_run:
            self._trace(f"    [dry-run] {method} {path}")
            if payload:
                self._trace("              " + json.dumps(payload, ensure_ascii=False))
            return {}

        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = raw
            code, message = _parse_detail(body)
            if exc.code in allow_status:
                return {"__status__": exc.code, "__code__": code, "__message__": message}
            raise ApiError(exc.code, code, message, step) from exc
        except urllib.error.URLError as exc:
            raise ApiError(0, "NETWORK_ERROR", f"API injoignable ({exc.reason})", step) from exc


def _slugify(value: str) -> str:
    table = str.maketrans("àâäáãåçèéêëìíîïñòóôöõùúûüýÿ", "aaaaaaceeeeiiiinooooouuuuyy")
    cleaned = value.lower().translate(table)
    keep = [char if char.isalnum() else "-" for char in cleaned]
    slug = "".join(keep)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


def _parse_service(value: str) -> dict:
    parts = [part.strip() for part in value.split("|")]
    if not parts or not parts[0]:
        raise SystemExit("--service attend 'Nom' ou 'Nom|Préfixe|Minutes'")
    service: dict = {"name": parts[0]}
    if len(parts) > 1 and parts[1]:
        service["ticket_prefix"] = parts[1].upper()
    if len(parts) > 2 and parts[2]:
        service["avg_duration_minutes"] = int(parts[2])
    if len(parts) > 3 and parts[3]:
        service["description"] = "|".join(parts[3:])
    return service


def _secret(cli_value: str | None, env_name: str, prompt: str) -> str:
    if cli_value:
        return cli_value
    from_env = os.getenv(env_name)
    if from_env:
        return from_env
    if not sys.stdin.isatty():
        raise SystemExit(f"Mot de passe manquant : passez-le en option ou via ${env_name}")
    return getpass.getpass(prompt)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="create_org.py",
        description="Crée une organisation TONDE complète via l'API (super-admin requis).",
    )
    parser.add_argument("--api", default=os.getenv("TONDE_API_URL", DEFAULT_API))
    parser.add_argument("--super-email", required=True, help="Compte SUPER_ADMIN existant")
    parser.add_argument("--super-password", help="Sinon $TONDE_SUPER_PASSWORD, sinon prompt")

    org = parser.add_argument_group("organisation")
    org.add_argument("--org-name", required=True)
    org.add_argument("--org-slug", help="Défaut : dérivé du nom (a-z0-9-)")
    org.add_argument("--sector", required=True, choices=SECTORS)
    org.add_argument("--description")
    org.add_argument("--country", default="Burundi")
    org.add_argument("--city")
    org.add_argument("--phone")
    org.add_argument("--email")
    org.add_argument("--website")
    org.add_argument("--logo-url")

    branch = parser.add_argument_group("première agence")
    branch.add_argument("--branch-name", required=True)
    branch.add_argument("--branch-slug", help="Défaut : dérivé du nom de l'agence")
    branch.add_argument("--branch-address")
    branch.add_argument("--branch-city")
    branch.add_argument("--branch-phone")
    branch.add_argument("--branch-email")
    branch.add_argument("--opens-at", default="08:00")
    branch.add_argument("--closes-at", default="17:00")

    parser.add_argument("--service", action="append", default=[], help="'Nom|Préfixe|Minutes' (répétable)")
    parser.add_argument("--counter", action="append", default=[], help="Nom du guichet (répétable)")

    admin = parser.add_argument_group("premier administrateur de l'organisation")
    admin.add_argument("--admin-name", required=True)
    admin.add_argument("--admin-email", required=True)
    admin.add_argument("--admin-password", help="Sinon $TONDE_ADMIN_PASSWORD, sinon prompt")
    admin.add_argument("--admin-counter", help="Nom du guichet à affecter à l'admin (optionnel)")

    parser.add_argument("--dry-run", action="store_true", help="Affiche les appels sans les envoyer")
    parser.add_argument("--json", action="store_true", help="Sortie JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = _build_parser().parse_args(argv)
    org_slug = args.org_slug or _slugify(args.org_name)
    branch_slug = args.branch_slug or _slugify(args.branch_name)
    services = [_parse_service(value) for value in args.service]
    counters = [name.strip() for name in args.counter if name.strip()]

    super_password = _secret(args.super_password, "TONDE_SUPER_PASSWORD", "Mot de passe SUPER_ADMIN : ")
    admin_password = _secret(args.admin_password, "TONDE_ADMIN_PASSWORD", "Mot de passe du futur admin : ")
    if not args.dry_run and len(admin_password) < 8:
        raise SystemExit("Le mot de passe admin doit contenir au moins 8 caractères")

    client = Client(args.api, dry_run=args.dry_run, quiet=args.json)
    summary: dict = {"api": args.api, "org_slug": org_slug, "services": [], "counters": []}

    def step(number: str, label: str) -> None:
        if not args.json:
            print(f"\n[{number}] {label}")

    # 1 — login super-admin
    step("1/6", f"Connexion {args.super_email}")
    login = client.call(
        "POST", "/api/v1/auth/login",
        payload={"email": args.super_email, "password": super_password},
        step="login", auth=False,
    )
    client.token = login.get("access_token", "")
    if not args.dry_run:
        role = (login.get("user") or {}).get("role")
        if role != "super_admin":
            raise SystemExit(
                f"Ce compte a le rôle '{role}', pas 'super_admin'. "
                "Utilisez un compte SUPER_ADMIN pour créer une organisation."
            )
        print(f"    connecté ({role})")

    # 2 — organisation
    step("2/6", f"Organisation « {args.org_name} » (slug {org_slug})")
    org_payload = {
        "name": args.org_name,
        "slug": org_slug,
        "sector": args.sector,
        "description": args.description,
        "country": args.country,
        "city": args.city,
        "phone": args.phone,
        "email": args.email,
        "website": args.website,
        "logo_url": args.logo_url,
    }
    org_payload = {key: value for key, value in org_payload.items() if value is not None}
    org_response = client.call(
        "POST", "/api/v1/organizations", payload=org_payload, step="organisation",
    )
    org_id = (org_response.get("data") or {}).get("id", "")
    summary["org_id"] = org_id
    if not args.dry_run:
        print(f"    org_id = {org_id}")

    # 3 — première agence
    step("3/6", f"Agence « {args.branch_name} » (slug {branch_slug})")
    branch_payload = {
        "name": args.branch_name,
        "slug": branch_slug,
        "sector": args.sector,
        "address": args.branch_address,
        "city": args.branch_city or args.city or "Bujumbura",
        "country": args.country,
        "phone": args.branch_phone or args.phone,
        "email": args.branch_email or args.email,
        "opens_at": args.opens_at,
        "closes_at": args.closes_at,
    }
    branch_payload = {key: value for key, value in branch_payload.items() if value is not None}
    branch_response = client.call(
        "POST", f"/api/v1/organizations/{org_id}/branches",
        payload=branch_payload, step="agence",
    )
    branch_id = (branch_response.get("data") or {}).get("id", "")
    summary["branch_id"] = branch_id
    if not args.dry_run:
        print(f"    branch_id = {branch_id}")

    # 4 — services
    step("4/6", f"Services ({len(services)})")
    counter_ids: dict[str, str] = {}
    for service in services:
        response = client.call(
            "POST", f"/api/v1/organizations/{org_id}/branches/{branch_id}/services",
            payload=service, step=f"service {service['name']}",
        )
        service_id = (response.get("data") or {}).get("id", "")
        summary["services"].append({"name": service["name"], "id": service_id})
        if not args.dry_run:
            print(f"    {service['name']} ({service.get('ticket_prefix', 'A')}) = {service_id}")

    # 5 — guichets
    step("5/6", f"Guichets ({len(counters)})")
    for name in counters:
        response = client.call(
            "POST", f"/api/v1/organizations/{org_id}/branches/{branch_id}/counters",
            payload={"name": name}, step=f"guichet {name}",
        )
        counter_id = (response.get("data") or {}).get("id", "")
        counter_ids[name] = counter_id
        summary["counters"].append({"name": name, "id": counter_id})
        if not args.dry_run:
            print(f"    {name} = {counter_id}")

    # 6 — compte admin + fiche employé
    step("6/6", f"Administrateur {args.admin_email}")
    register = client.call(
        "POST", "/api/v1/auth/register/email",
        payload={"email": args.admin_email, "password": admin_password, "name": args.admin_name},
        step="compte admin", auth=False, allow_status=(409,),
    )
    if register.get("__status__") == 409:
        # Compte déjà existant : on récupère son id en s'y connectant.
        existing = client.call(
            "POST", "/api/v1/auth/login",
            payload={"email": args.admin_email, "password": admin_password},
            step="compte admin existant", auth=False,
        )
        admin_user_id = (existing.get("user") or {}).get("id", "")
        if not args.dry_run:
            print("    compte déjà existant, réutilisé")
    else:
        admin_user_id = (register.get("user") or {}).get("id", "")
        if not args.dry_run:
            print(f"    user_id = {admin_user_id}")

    employee_payload = {
        "user_id": admin_user_id,
        "role": "admin_org",
        "agency_id": branch_id,
    }
    if args.admin_counter:
        counter_id = counter_ids.get(args.admin_counter)
        if args.admin_counter not in counter_ids:
            raise SystemExit(
                f"--admin-counter '{args.admin_counter}' ne correspond à aucun --counter "
                f"({', '.join(counter_ids) or 'aucun'})"
            )
        employee_payload["counter_id"] = counter_id

    client.call(
        "POST", f"/api/v1/organizations/{org_id}/employees",
        payload=employee_payload, step="employé admin", allow_status=(409,),
    )
    summary["admin_email"] = args.admin_email
    summary["admin_user_id"] = admin_user_id

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    print("\n" + "=" * 62)
    print(f"Organisation prête : {args.org_name} ({org_slug})")
    print(f"  org_id     : {org_id}")
    print(f"  branch_id  : {branch_id}")
    print(f"  admin      : {args.admin_email} (role admin_org)")
    print("=" * 62)
    print("L'admin doit se RECONNECTER : son token précédent (role client)")
    print("ne donne pas les droits admin_org.")
    print(f"  curl -X POST {args.api}/api/v1/auth/login \\")
    print(f"       -H 'Content-Type: application/json' \\")
    print(f"       -d '{{\"email\":\"{args.admin_email}\",\"password\":\"...\"}}'")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ApiError as exc:
        print(f"\nECHEC {exc}", file=sys.stderr)
        raise SystemExit(1)
