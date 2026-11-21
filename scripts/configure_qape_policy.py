"""
Crée une AbusePolicy sur une organisation DEJA EXISTANTE (production-safe).

Contrairement à activate_qape_local.py, ce script NE crée NI organisation, NI
branche, NI service, NI compte. Il ne fait que retrouver l'org et écrire la
politique. Pensé pour être exécuté contre la base de production.

Défaut = DRY RUN (aucune écriture). Écriture nécessite --apply,
--confirm-database <nom exact> et --org-slug OU --org-name.

Exemples:
  # 1) Voir ce qui serait fait (org entière)
  python -m scripts.configure_qape_policy --org-slug ulbu

  # 2) Appliquer sur toute l'organisation
  python -m scripts.configure_qape_policy --org-slug ulbu \
      --apply --confirm-database tonde_db

  # 3) Appliquer sur un seul service (le préfixe est affiché par le dry-run)
  python -m scripts.configure_qape_policy --org-slug ulbu \
      --service-code A --apply --confirm-database tonde_db

Pour changer les seuils ensuite : augmenter --version (ex. 2) et rejouer.
La version précédente est désactivée automatiquement dans la même transaction.
"""
import argparse
import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import text

from app.core.database import AsyncSessionLocal, engine
from app.schemas.qape import PolicyConfig, PolicyInput
from app.services.qape_data import PolicyRepository, QapeScopeError

# Valeurs par défaut — à ajuster selon la décision de l'admin.
DEFAULT_CONFIG = PolicyConfig(
    limits={
        "bucket_capacity": 3,
        "bucket_refill_seconds": 30.0,
        "max_requests_per_10m": 3,
        "max_tickets_per_day": 5,
    },
    risk={
        "review_threshold": 4,
        "cooldown_seconds": 60,
        "hard_block_seconds": 300,
    },
    counting={"timezone": "Africa/Bujumbura"},
    staff_override={"enabled": True, "require_reason": True},
    redis_unavailable="REVIEW_REQUIRED",
)


async def main(args) -> None:
    async with AsyncSessionLocal() as session:
        db_name = (await session.execute(text("SELECT current_database()"))).scalar_one()
        await session.rollback()

        if args.apply and args.confirm_database != db_name:
            raise SystemExit(
                f"Refusé : --confirm-database doit valoir {db_name!r} exactement."
            )
        if not args.org_slug and not args.org_name:
            raise SystemExit("Indiquez --org-slug ou --org-name (jamais de devinette).")

        # Retrouver l'org SANS rien créer.
        if args.org_slug:
            org = (await session.execute(
                text("SELECT id, name, slug FROM organizations WHERE slug = :s"),
                {"s": args.org_slug},
            )).first()
        else:
            org = (await session.execute(
                text("SELECT id, name, slug FROM organizations WHERE lower(name) = lower(:n)"),
                {"n": args.org_name},
            )).first()

        if org is None:
            raise SystemExit("Organisation introuvable. Vérifiez le slug/nom exact.")
        org_id, org_name, org_slug = org
        print(f"Organisation trouvée : {org_name} (slug={org_slug}, id={org_id})")

        # Services disposibles (information pour l'admin).
        services = (await session.execute(
            text("SELECT id, name, ticket_prefix FROM services WHERE org_id = :o ORDER BY name"),
            {"o": org_id},
        )).all()
        print("Services de l'organisation :")
        for sid, sname, scode in services:
            print(f"  - {sname} (prefix={scode}, id={sid})")
        if not services:
            print("  (aucun)")

        service_id = None
        if args.service_code:
            match = [s for s in services if s[2] and s[2].upper() == args.service_code.upper()]
            if not match:
                raise SystemExit(f"Préfix service {args.service_code!r} introuvable dans cette org.")
            service_id = match[0][0]
            print(f"Scope retenu : service {match[0][1]} ({service_id})")
        else:
            print("Scope retenu : TOUTE l'organisation (politique par défaut)")

        repo = PolicyRepository(session)
        effective = await repo.effective(org_id, service_id)
        if effective:
            print(
                f"Politique active actuelle : version {effective.version}, "
                f"id={effective.id}, creee_le={effective.created_at}"
            )
        else:
            print("Aucune politique active sur ce scope (QAPE inactif ici).")

        data = PolicyInput(
            org_id=org_id,
            service_id=service_id,
            version=args.version,
            enabled=True,
            config=DEFAULT_CONFIG,
            created_by=None,
        )

        print("\nConfiguration proposée :")
        print(json.dumps(data.config.model_dump(), indent=2, ensure_ascii=False))

        if not args.apply:
            print("\nDRY RUN — rien n'a été écrit. Relancez avec --apply --confirm-database ...")
            await session.rollback()
            await engine.dispose()
            return

        created = await repo.create(data)
        await repo.activate(org_id, created.id)  # désactive l'ancienne version, même transaction
        await session.commit()
        print(f"\nAPPLIQUÉ : AbusePolicy version {args.version} active (id={created.id}).")
        print("QAPE est maintenant actif sur ce scope.")
        await engine.dispose()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--org-slug", help="Slug exact de l'organisation (ex: ulbu)")
    p.add_argument("--org-name", help="Nom exact de l'organisation")
    p.add_argument("--service-code", help="Optionnel : préfix du service (ex: A, B) pour limiter la politique")
    p.add_argument("--version", type=int, default=1, help="Version de la politique (1, 2, 3...)")
    p.add_argument("--apply", action="store_true", help="Écrire en base (défaut: dry-run)")
    p.add_argument("--confirm-database", help="Obligatoire avec --apply")
    args = p.parse_args()
    asyncio.run(main(args))
