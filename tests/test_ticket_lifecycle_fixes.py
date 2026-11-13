"""
Tests de régression — Cycle de vie des tickets.

Couvre quatre défauts corrigés :

1. Numérotation : `COUNT + 1` produisait des « A-5 » en double à l'heure de
   pointe. La séquence passe désormais par un compteur Redis atomique, avec
   index unique en base comme dernier rempart.

2. Visibilité : `get_ticket()` appliquait le contrôle de propriété à TOUS les
   rôles — un agent ne pouvait pas consulter le ticket qu'il venait d'appeler.
   Le staff accède maintenant aux tickets de son organisation.

3. Lockout : TRANSFERRED et INCOMPLETE étaient dans ACTIVE_STATUSES alors que
   ce sont des états terminaux → client bloqué à vie. Ils en sont retirés, et
   ABSENT devient annulable pour offrir une sortie.

4. Atomicité : si Redis tombait après le commit, le ticket restait WAITING en
   base sans être dans aucune file (fantôme). Il est désormais annulé et
   l'appelant reçoit un 503 explicite.
"""
import pytest
from unittest.mock import patch, AsyncMock
from fastapi import HTTPException

from app.models.organization import Organization
from app.models.branch import Branch, Service
from app.models.ticket import Ticket, TicketStatus
from app.models.user import User, UserRole
from app.schemas.ticket import CreateTicketRequest
from app.services.branch_service import BranchService


# ── Helpers ───────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_queue(mock_redis):
    async def _fake_next_sequence(agency_id, prefix, day, db_max):
        return db_max + 1

    with patch("app.services.ticket_service.next_daily_sequence", side_effect=_fake_next_sequence):
        with patch("app.services.ticket_service.add_to_queue", new_callable=AsyncMock, return_value=1):
            with patch("app.services.ticket_service.get_queue_size", new_callable=AsyncMock, return_value=1):
                with patch("app.services.ticket_service.remove_from_queue", new_callable=AsyncMock):
                    with patch("app.services.ticket_service.get_next_ticket", new_callable=AsyncMock):
                        with patch("app.services.ticket_service.get_queue_position", new_callable=AsyncMock, return_value=1):
                            yield


async def _setup(db_session):
    """Crée org + branch ouverte + service + un client."""
    org = Organization(name="Hopital Test", slug="hopital-test", sector="hospital")
    db_session.add(org)
    await db_session.commit()
    await db_session.refresh(org)

    branch = Branch(
        org_id=org.id,
        name="Accueil",
        slug=f"accueil-{org.id[:8]}",
        sector="hospital",
        is_active=True,
        is_open=True,
    )
    db_session.add(branch)
    await db_session.commit()
    await db_session.refresh(branch)

    service = Service(
        org_id=org.id,
        branch_id=branch.id,
        name="Consultation",
        ticket_prefix="A",
        avg_duration_minutes=5,
    )
    db_session.add(service)
    await db_session.commit()
    await db_session.refresh(service)

    client = User(phone="+25779000111", is_active=True)
    db_session.add(client)
    await db_session.commit()
    await db_session.refresh(client)

    return org, branch, service, client


def _req(branch_id: str, service_id: str) -> CreateTicketRequest:
    return CreateTicketRequest(agency_id=branch_id, service_id=service_id)


async def _create(ticket_service, branch, service, user_id, org_id=None):
    return await ticket_service.create_ticket(
        _req(branch.id, service.id),
        user_id=user_id,
        org_id=org_id,
        actor_role=UserRole.CLIENT,
    )


# ── 1. Numérotation ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ticket_numbers_increment(ticket_service, db_session, mock_queue):
    """Deux tickets successifs ne portent jamais le même numéro."""
    org, branch, service, _ = await _setup(db_session)

    first = await _create(ticket_service, branch, service, "u-num-1", org.id)
    await ticket_service.cancel_ticket(first.id, "u-num-1", org.id)
    second = await _create(ticket_service, branch, service, "u-num-1", org.id)

    assert first.number == "A-1"
    assert second.number == "A-2"
    assert first.number != second.number


@pytest.mark.asyncio
async def test_next_daily_sequence_is_atomic(monkeypatch):
    """
    Le compteur Redis ne recycle jamais un numéro déjà distribué, même si le
    ticket correspondant est supprimé (contrairement à un COUNT en base).
    """
    from app.core.redis import next_daily_sequence

    store: dict[str, int] = {}

    class FakeRedis:
        async def set(self, key, value, nx=False, ex=None):
            if nx and key in store:
                return False
            store[key] = int(value)
            return True

        async def incr(self, key):
            store[key] = store.get(key, 0) + 1
            return store[key]

    monkeypatch.setattr(
        "app.core.redis.get_redis", AsyncMock(return_value=FakeRedis())
    )

    seqs = [
        await next_daily_sequence("agency-1", "A", "2026-11-13", db_max=5)
        for _ in range(3)
    ]
    assert seqs == [6, 7, 8]

    # L'amorce n'est posée qu'une fois : un db_max plus faible ne remet pas
    # le compteur en arrière (c'était le risque d'un simple SET).
    seq_after = await next_daily_sequence("agency-1", "A", "2026-11-13", db_max=0)
    assert seq_after == 9


# ── 2. Visibilité du ticket pour le staff ────────────────────────────────────

@pytest.mark.asyncio
async def test_staff_can_read_client_ticket(ticket_service, db_session, mock_queue):
    """Un agent de la même organisation consulte le ticket d'un client."""
    org, branch, service, client = await _setup(db_session)
    ticket = await _create(ticket_service, branch, service, client.id, org.id)

    result = await ticket_service.get_ticket(
        ticket.id,
        user_id="agent-user",
        org_id=org.id,
        actor_role=UserRole.AGENT,
    )

    assert result.id == ticket.id
    assert result.number == ticket.number


@pytest.mark.asyncio
async def test_client_still_cannot_read_other_client_ticket(ticket_service, db_session, mock_queue):
    """Un client reste limité à ses propres tickets (403)."""
    org, branch, service, client = await _setup(db_session)
    ticket = await _create(ticket_service, branch, service, client.id, org.id)

    with pytest.raises(HTTPException) as exc_info:
        await ticket_service.get_ticket(
            ticket.id,
            user_id="autre-client",
            org_id=org.id,
            actor_role=UserRole.CLIENT,
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_staff_cannot_read_ticket_from_another_org(ticket_service, db_session, mock_queue):
    """Le staff reste cloisonné à son organisation (404, pas de fuite)."""
    org, branch, service, client = await _setup(db_session)
    ticket = await _create(ticket_service, branch, service, client.id, org.id)

    with pytest.raises(HTTPException) as exc_info:
        await ticket_service.get_ticket(
            ticket.id,
            user_id="agent-autre-org",
            org_id="org-concurrente",
            actor_role=UserRole.AGENT,
        )

    assert exc_info.value.status_code == 404


# ── 3. Lockout ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_transferred_ticket_does_not_block_new_ticket(ticket_service, db_session, mock_queue):
    """Après un transfert (état terminal), le client peut reprendre un ticket."""
    org, branch, service, client = await _setup(db_session)
    first = await _create(ticket_service, branch, service, client.id, org.id)

    # TRANSFERRED n'est atteignable par aucune route : on force l'état
    from sqlalchemy import select
    result = await db_session.execute(select(Ticket).where(Ticket.id == first.id))
    row = result.scalar_one()
    row.status = TicketStatus.TRANSFERRED
    await db_session.commit()

    second = await _create(ticket_service, branch, service, client.id, org.id)
    assert second.status == "waiting"


@pytest.mark.asyncio
async def test_absent_ticket_can_be_cancelled(ticket_service, db_session, mock_queue):
    """ABSENT → CANCELLED est autorisé : le client n'est plus prisonnier."""
    org, branch, service, client = await _setup(db_session)
    ticket = await _create(ticket_service, branch, service, client.id, org.id)

    from sqlalchemy import select
    result = await db_session.execute(select(Ticket).where(Ticket.id == ticket.id))
    row = result.scalar_one()
    row.status = TicketStatus.CALLED
    await db_session.commit()
    await ticket_service.mark_absent(ticket.id, org.id)

    await ticket_service.cancel_ticket(ticket.id, client.id, org.id)

    result = await db_session.execute(select(Ticket).where(Ticket.id == ticket.id))
    assert result.scalar_one().status == TicketStatus.CANCELLED


# ── 4. Atomicité de la création ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_redis_failure_cancels_ticket_and_returns_503(ticket_service, db_session, mock_redis):
    """Si la file Redis est indisponible, aucun ticket fantôme ne subsiste."""
    org, branch, service, client = await _setup(db_session)

    with patch(
        "app.services.ticket_service.add_to_queue",
        new_callable=AsyncMock,
        side_effect=ConnectionError("redis down"),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await _create(ticket_service, branch, service, client.id, org.id)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "QUEUE_UNAVAILABLE"

    from sqlalchemy import select
    result = await db_session.execute(
        select(Ticket).where(Ticket.user_id == client.id)
    )
    tickets = result.scalars().all()
    assert len(tickets) == 1
    assert tickets[0].status == TicketStatus.CANCELLED, "le ticket fantôme doit être annulé"


# ── 5. Super-admin sur les branches (vérification) ───────────────────────────

@pytest.mark.asyncio
async def test_super_admin_can_read_branch(db_session):
    """
    Un SUPER_ADMIN n'a pas d'org_id : BranchService doit interpréter None
    comme « pas de filtre » et non comme « org inconnue ».
    """
    org, branch, _, _ = await _setup(db_session)

    service = BranchService(db_session)
    result = await service.get_by_id(branch.id, caller_org_id=None)

    assert result.id == branch.id
