"""
Activation locale de QAPE — crée les données minimales et la AbusePolicy.

Prérequis : migrations appliquées (`alembic upgrade head`).

Usage:
    python -m scripts.activate_qape_local

Idempotent : ne recrée que ce qui manque. Affiche un récapitulatif à la fin.

Ce script est un utilitaire de DEVELOPPEMENT / DEMO. En production, les
organisations, branches, services et comptes sont créés via l'API admin, et
la politique est validée avec PolicyInput (app.schemas.qape) avant insertion.
"""
import asyncio
import json
import uuid

from sqlalchemy import text

from app.core.database import AsyncSessionLocal, engine

# Configuration de la politique — seuils volontairement conservateurs.
# Un même utilisateur peut prendre 3 tickets rapidement (bucket), 3 demandes
# par 10 minutes (anti-spam), 5 tickets par jour (quota), sinon il passe en
# revue humaine plutôt que d'être bloqué sèchement.
POLICY_CONFIG = {
    "limits": {
        "bucket_capacity": 3,
        "bucket_refill_seconds": 30.0,
        "max_requests_per_10m": 3,
        "max_tickets_per_day": 5,
    },
    "risk": {
        "review_threshold": 4,
        "cooldown_seconds": 60,
        "hard_block_seconds": 300,
    },
    "counting": {
        "timezone": "Africa/Bujumbura",
    },
    "staff_override": {
        "enabled": True,
        "require_reason": True,
    },
    "redis_unavailable": "REVIEW_REQUIRED",
}


async def first_or_create(session, sql: str, params: dict, label: str) -> str:
    """Retourne l'id existant ou crée la ligne. Assume que sql SELECT ... LIMIT 1."""
    select_sql = sql[0]
    insert_sql = sql[1]
    row = (await session.execute(text(select_sql), params)).first()
    if row:
        print(f"  {label} existant: {row[1]} ({row[0]})")
        return row[0]
    new_id = str(uuid.uuid4())
    await session.execute(text(insert_sql), {"id": new_id, **params})
    print(f"  {label} créé: ({new_id})")
    return new_id


async def main() -> None:
    async with AsyncSessionLocal() as session:
        # 1. Organisation — `sector` est NOT NULL et sans défaut.
        org_id = await first_or_create(
            session,
            (
                "SELECT id, name FROM organizations LIMIT 1",
                """INSERT INTO organizations (id, name, slug, sector, country, is_active, created_at, updated_at)
                   VALUES (:id, :name, :slug, :sector, 'Burundi', true, now(), now())""",
            ),
            {"name": "ULBU Banque", "slug": "ulbu-banque", "sector": "finance"},
            "Organisation",
        )

        # 2. Branche — la plupart des colonnes ont des défauts ; pas de colonne timezone.
        branch_id = await first_or_create(
            session,
            (
                "SELECT id, name FROM branches WHERE org_id = :org_id LIMIT 1",
                """INSERT INTO branches (id, org_id, name, slug, sector, created_at, updated_at)
                   VALUES (:id, :org_id, :name, :slug, :sector, now(), now())""",
            ),
            {
                "org_id": org_id,
                "name": "Agence Principale Bujumbura",
                "slug": "ulbu-bjz-principale",
                "sector": "finance",
            },
            "Branche",
        )

        # 3. Service — `ticket_prefix` a un défaut ('A').
        service_id = await first_or_create(
            session,
            (
                "SELECT id, name FROM services WHERE branch_id = :branch_id LIMIT 1",
                """INSERT INTO services (id, org_id, branch_id, name, created_at)
                   VALUES (:id, :org_id, :branch_id, :name, now())""",
            ),
            {"org_id": org_id, "branch_id": branch_id, "name": "Caisse"},
            "Service",
        )

        # 4. Compte admin (utilisé comme created_by de la politique).
        # SQLAlchemy persiste le NOM du membre de l'enum (ADMIN_ORG), pas sa valeur.
        admin_row = (await session.execute(
            text("SELECT id FROM users WHERE role = 'ADMIN_ORG' AND email = :email LIMIT 1"),
            {"email": "admin@ulbu.bi"},
        )).first()
        if admin_row:
            admin_id = admin_row[0]
            print(f"  Admin existant: ({admin_id})")
        else:
            admin_id = str(uuid.uuid4())
            try:
                from passlib.context import CryptContext
                hashed = CryptContext(schemes=["bcrypt"], deprecated="auto").hash("admin123")
            except Exception:  # passlib/bcrypt indisponible → pas de mot de passe
                hashed = None
            await session.execute(
                text("""INSERT INTO users (id, org_id, name, email, phone, hashed_password, role, created_at, updated_at)
                        VALUES (:id, :org_id, :name, :email, :phone, :hashed, 'ADMIN_ORG', now(), now())"""),
                {
                    "id": admin_id,
                    "org_id": org_id,
                    "name": "Vital Admin",
                    "email": "admin@ulbu.bi",
                    "phone": "+25779000000",
                    "hashed": hashed,
                },
            )
            print(f"  Admin créé: admin@ulbu.bi / admin123 ({admin_id})")

        # 5. AbusePolicy — une seule active par service (index uq_abuse_policy_active_service).
        policy = (await session.execute(
            text("""SELECT id, version FROM abuse_policies
                    WHERE org_id = :org_id AND service_id = :service_id AND enabled = true LIMIT 1"""),
            {"org_id": org_id, "service_id": service_id},
        )).first()
        if policy:
            print(f"  AbusePolicy existante: version {policy[1]} ({policy[0]})")
        else:
            policy_id = str(uuid.uuid4())
            await session.execute(
                text("""INSERT INTO abuse_policies
                        (id, org_id, service_id, version, enabled, config, created_by, created_at, updated_at)
                        VALUES (:id, :org_id, :service_id, 1, true, CAST(:config AS jsonb), :created_by, now(), now())"""),
                {
                    "id": policy_id,
                    "org_id": org_id,
                    "service_id": service_id,
                    "config": json.dumps(POLICY_CONFIG),
                    "created_by": admin_id,
                },
            )
            print(f"  AbusePolicy créée ({policy_id})")

        await session.commit()

        print("\n=== Récapitulatif ===")
        print(f"  Organisation : ULBU Banque ({org_id})")
        print(f"  Branche      : Agence Principale Bujumbura ({branch_id})")
        print(f"  Service      : Caisse ({service_id})")
        print(f"  Admin        : admin@ulbu.bi / admin123 ({admin_id})")
        print("  QAPE         : ACTIVÉ sur le service Caisse")
        print("\n  Paramètres:")
        for key, value in POLICY_CONFIG["limits"].items():
            print(f"    limits.{key} = {value}")
        for key, value in POLICY_CONFIG["risk"].items():
            print(f"    risk.{key} = {value}")
        print(f"    counting.timezone = {POLICY_CONFIG['counting']['timezone']}")
        print(f"    redis_unavailable = {POLICY_CONFIG['redis_unavailable']}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
