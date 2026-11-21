"""
Active QAPE sur TOUTES les organisations qui n'ont pas encore de politique.

Idempotent : une org avec deja une politique active org-wide est ignoree.
Passe par PolicyInput (validation Pydantic) + PolicyRepository.create().
Dry-run par defaut. Ecriture avec --apply + --confirm-database.

Concu pour etre relance apres chaque creation d'organisation : les orgs
futures recevront automatiquement la meme politique organisation-wide.

ATTENTION : utiliser l'URL DIRECTE Neon (pas le pooler) pour les ecritures,
conformement a la recommandation Render/Neon pour les migrations et ecritures
de schema/session.

Usage:
  QAPE_DATABASE_URL="<url directe>" python -m scripts.enable_qape_all_orgs
  QAPE_DATABASE_URL="<url directe>" python -m scripts.enable_qape_all_orgs \
      --apply --confirm-database neondb
"""
import argparse
import asyncio
import json
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.schemas.qape import PolicyConfig, PolicyInput
from app.services.qape_data import PolicyRepository


def default_config() -> PolicyConfig:
    return PolicyConfig(
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


def to_asyncpg_url(raw: str) -> str:
    """Neon fournit sslmode/channel_binding (params libpq) : asyncpg les refuse."""
    url = raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    for param in ("sslmode", "channel_binding"):
        if f"{param}=" in url:
            import re
            url = re.sub(rf"[?&]{param}=[^&]*", "", url, count=1)
    return url


async def main(args) -> None:
    raw = os.environ.get("QAPE_DATABASE_URL")
    if not raw:
        raise SystemExit("QAPE_DATABASE_URL manquante (URL DIRECTE Neon).")

    engine = create_async_engine(to_asyncpg_url(raw), echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        db_name = (await session.execute(text("SELECT current_database()"))).scalar_one()
        await session.rollback()
        print(f"Base: {db_name} (dry_run={not args.apply})")

        if args.apply and args.confirm_database != db_name:
            raise SystemExit(
                f"Refuse : --confirm-database doit valoir {db_name!r} exactement."
            )

        orgs = (await session.execute(
            text("SELECT id, name, slug FROM organizations ORDER BY name")
        )).all()
        if not orgs:
            raise SystemExit("Aucune organisation trouvee.")

        repo = PolicyRepository(session)
        config = default_config()
        activated, skipped = [], []

        for org_id, org_name, org_slug in orgs:
            existing = await repo.effective(org_id, None)
            if existing is not None:
                skipped.append((org_slug, existing.version, existing.id))
                print(f"  - {org_slug}: DEJA ACTIVE (version {existing.version}) -> ignoree")
                continue

            data = PolicyInput(
                org_id=org_id,
                service_id=None,          # org-wide : couvre tous les services
                version=1,
                enabled=True,
                config=config,
                created_by=None,
            )
            if not args.apply:
                print(f"  + {org_slug}: CREERAIT une politique org-wide (version 1)")
                activated.append((org_slug, "DRY-RUN"))
                continue

            row = await repo.create(data)
            await repo.activate(org_id, row.id)
            activated.append((org_slug, row.id))
            print(f"  + {org_slug}: ACTIVEE (id={row.id})")

        if args.apply:
            await session.commit()
        else:
            await session.rollback()

        print("\n=== Resume ===")
        print(f"Organisations examinees : {len(orgs)}")
        print(f"Activees {'ou prevues' if not args.apply else ''} : {len(activated)}")
        print(f"Deja actives (ignorees) : {len(skipped)}")
        if not args.apply:
            print("DRY RUN — rien ecrit. Relancer avec --apply --confirm-database <nom>.")
        print("\nConfiguration appliquee a chaque org:")
        print(json.dumps(config.model_dump(), indent=2, ensure_ascii=False))

    await engine.dispose()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="Ecrire en base")
    p.add_argument("--confirm-database", help="Obligatoire avec --apply")
    args = p.parse_args()
    asyncio.run(main(args))
