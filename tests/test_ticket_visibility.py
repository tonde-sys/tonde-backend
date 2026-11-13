"""
Tests de régression — visibilité et cycle de vie des tickets.

Couvre deux défauts qui ont survécu à la refonte du moteur de file :

1. Visibilité : `get_ticket()` appliquait le contrôle de propriété à TOUS les
   rôles. Un guichetier ne pouvait donc pas ouvrir le ticket qu'il venait
   d'appeler (403 systématique), ce qui cassait tout l'enchaînement
   /serving → /done côté desktop. Le staff accède désormais aux tickets de son
   organisation ; le client reste limité aux siens.

2. Lockout : ABSENT doit pouvoir être annulé, sinon un client qui rate son
   tour reste bloqué par la règle « un seul ticket actif ».

Le cloisonnement multi-tenant est vérifié dans les deux sens : un agent d'une
autre organisation reçoit un 404 (pas de fuite d'existence).
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.branch import Branch, Service
from app.models.organization import Organization
from app.models.ticket import Ticket, TicketStatus
from app.models.user import User, UserRole
from app.schemas.ticket import CreateTicketRequest
from app.services.branch_service import BranchService
from app.services.ticket_service import TicketService


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_queue(mock_redis):
    """Neutralise Redis : séquence déterministe, file acceptée."""
    with patch(
        "app.services.ticket_service.next_ticket_sequence",
        new_callable=AsyncMock,
        side_effect=lambda agency_id, prefix, day, load_seed: 1,
    ), patch(
        "app.services.ticket_service.acquire_ticket_creation_lock",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "app.services.ticket_service.release_ticket_creation_lock",
        new_callable=AsyncMock,
    ), patch(
        "app.services.ticket_service.add_to_queue",
        new_callable=AsyncMock,
        return_value=1,
    ), patch(
        "app.services.ticket_service.add_to_queue_if_absent",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "app.services.ticket_service.get_queue_size",
        new_callable=AsyncMock,
        return_value=1,
    ), patch(
        "app.services.ticket_service.get_queue_position",
        new_callable=AsyncMock,
        return_value=1,
    ), patch(
        "app.services.ticket_service.remove_from_queue",
        new_callable=AsyncMock,
    ), patch(
        "app.services.ticket_service.set_deadline",
        new_callable=AsyncMock,
    ), patch(
        "app.services.ticket_service.clear_deadlines",
        new_callable=AsyncMock,
    ):
        yield


async def _setup(db_session):
    """Crée une organisation, une branch ouverte, un service et un client."""
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


async def _create(ticket_service: TicketService, branch, service, user_id, org_id):
    return await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=branch.id, service_id=service.id),
        user_id=user_id,
        org_id=org_id,
    )


# ── Visibilité ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_staff_can_read_client_ticket(ticket_service, db_session, mock_queue):
    """Un agent de la même organisation consulte le ticket d'un client."""
    org, branch, service, client = await _setup(db_session)
    ticket = await _create(ticket_service, branch, service, client.id, org.id)

    result = await ticket_service.get_ticket(
        ticket.id, user_id="agent-user", org_id=org.id, actor_role=UserRole.AGENT
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
            ticket.id, user_id="autre-client", org_id=org.id, actor_role=UserRole.CLIENT
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


# ── Lockout ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_absent_ticket_can_be_cancelled(ticket_service, db_session, mock_queue):
    """ABSENT → CANCELLED est autorisé : le client n'est plus prisonnier."""
    org, branch, service, client = await _setup(db_session)
    ticket = await _create(ticket_service, branch, service, client.id, org.id)

    # Un agent a marqué le client absent (l'affectation guichet est vérifiée
    # ailleurs) : on force l'état pour isoler la transition qui nous intéresse.
    row = (await db_session.execute(select(Ticket).where(Ticket.id == ticket.id))).scalar_one()
    row.status = TicketStatus.ABSENT
    await db_session.commit()

    await ticket_service.cancel_ticket(ticket.id, client.id, org.id)

    final = (await db_session.execute(select(Ticket).where(Ticket.id == ticket.id))).scalar_one()
    assert final.status == TicketStatus.CANCELLED


# ── Super-admin sur les branches ─────────────────────────────────────────────

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
