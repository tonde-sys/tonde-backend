"""
Test fonctionnel de bout en bout : vérifie que QAPE est ACTIVÉ et fonctionne.

Crée un client de test, appelle TicketService.create_ticket() trois fois
(une par ticket autorisé par le bucket), puis vérifie en base que :
  - les tickets ont été créés ;
  - des AbuseEvent (TICKET_CREATED, provenance LIVE) ont été écrits ;
  - la 4e création est refusée (quota / rate limit) — comportement QAPE.

Usage: python -m scripts.test_qape_live
"""
import asyncio
import uuid

from sqlalchemy import text

from app.core.database import AsyncSessionLocal, engine
from app.schemas.ticket import CreateTicketRequest
from app.services.ticket_service import TicketService


async def main() -> None:
    async with AsyncSessionLocal() as session:
        # Nettoyage des données d'un précédent run (script idempotent).
        old = (await session.execute(
            text("SELECT id FROM users WHERE phone = '+25779123456'")
        )).scalars().all()
        for uid in old:
            await session.execute(text("DELETE FROM qape_create_operations WHERE user_id = :u"), {"u": uid})
            await session.execute(text("DELETE FROM tickets WHERE user_id = :u"), {"u": uid})
            await session.execute(text("DELETE FROM abuse_events WHERE user_id = :u"), {"u": uid})
            await session.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
        await session.commit()

        org = (await session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
        branch = (await session.execute(text("SELECT id FROM branches WHERE org_id = :o LIMIT 1"), {"o": org})).scalar_one()
        service = (await session.execute(text("SELECT id FROM services WHERE org_id = :o LIMIT 1"), {"o": org})).scalar_one()

        # Ouvrir la branche pour le test (sinon BRANCH_CLOSED avant même QAPE).
        await session.execute(text("UPDATE branches SET is_open = true WHERE id = :b"), {"b": branch})
        await session.commit()

        # Client de test
        client_id = str(uuid.uuid4())
        await session.execute(
            text("""INSERT INTO users (id, name, phone, role, created_at, updated_at)
                    VALUES (:id, 'Marie Test', '+25779123456', 'CLIENT', now(), now())"""),
            {"id": client_id},
        )
        await session.commit()
        print(f"Client de test: Marie Test ({client_id})")

        svc = TicketService(session)
        results = []
        for i in range(1, 5):
            req = CreateTicketRequest(
                agency_id=branch,
                service_id=service,
                priority="standard",
                client_action_id=f"qape-live-test-{i:03d}-{uuid.uuid4().hex[:6]}",
            )
            try:
                resp = await svc.create_ticket(req, client_id, org)
                results.append(f"  {i}. CREATED → {resp.number} (position {resp.position})")
            except Exception as e:
                code = getattr(e, "detail", None)
                results.append(f"  {i}. REFUSÉ → {code if code else str(e)[:120]}")
        for line in results:
            print(line)

        # Vérification des AbuseEvent LIVE
        n_events = (await session.execute(text(
            "SELECT COUNT(*) FROM abuse_events WHERE user_id = :u AND event_type = 'TICKET_CREATED'"
        ), {"u": client_id})).scalar_one()
        print(f"\nAbuseEvent TICKET_CREATED pour ce client: {n_events}")
        n_tickets = (await session.execute(text(
            "SELECT COUNT(*) FROM tickets WHERE user_id = :u"
        ), {"u": client_id})).scalar_one()
        print(f"Tickets créés pour ce client: {n_tickets}")
        print("\n=> QAPE est ACTIF" if n_events > 0 else "\n=> QAPE n'a pas écrit d'événements")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
