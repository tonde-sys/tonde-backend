"""
Tests de régression — Priorités des tickets.

Bug corrigé : n'importe quel client pouvait envoyer `priority="emergency"`
dans le corps de la requête et passer devant toute la file d'attente.
La priorité est désormais validée contre le rôle de l'appelant.

Règle :
  - CLIENT (ou appelant inconnu) → standard, priority
  - AGENT et au-dessus          → standard, priority, vip, emergency
"""
import pytest
from unittest.mock import patch, AsyncMock
from fastapi import HTTPException

from app.models.organization import Organization
from app.models.branch import Branch, Service
from app.models.ticket import TicketPriority, Ticket
from app.models.user import UserRole
from app.schemas.ticket import CreateTicketRequest


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_queue(mock_redis):
    """Mock des helpers Redis de file d'attente."""
    async def _fake_next_sequence(agency_id, prefix, day, db_max):
        return db_max + 1

    with patch("app.services.ticket_service.next_daily_sequence", side_effect=_fake_next_sequence):
        with patch("app.services.ticket_service.add_to_queue", new_callable=AsyncMock, return_value=1):
            with patch("app.services.ticket_service.get_queue_size", new_callable=AsyncMock, return_value=1):
                with patch("app.services.ticket_service.remove_from_queue", new_callable=AsyncMock):
                    with patch("app.services.ticket_service.get_next_ticket", new_callable=AsyncMock):
                        with patch("app.services.ticket_service.get_queue_position", new_callable=AsyncMock, return_value=1):
                            yield


async def _setup_branch(db_session):
    """Crée org + branch ouverte + service, et retourne (org, branch, service)."""
    org = Organization(name="Clinique Test", slug="clinique-test", sector="hospital")
    db_session.add(org)
    await db_session.commit()
    await db_session.refresh(org)

    branch = Branch(
        org_id=org.id,
        name="Urgences",
        slug=f"urgences-{org.id[:8]}",
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
        ticket_prefix="C",
        avg_duration_minutes=5,
    )
    db_session.add(service)
    await db_session.commit()
    await db_session.refresh(service)
    return org, branch, service


def _request(branch_id: str, service_id: str, priority: str) -> CreateTicketRequest:
    return CreateTicketRequest(
        agency_id=branch_id,
        service_id=service_id,
        priority=priority,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_client_cannot_self_assign_emergency(ticket_service, db_session, mock_queue):
    """Un client qui demande 'emergency' est refusé (403) et aucun ticket n'est créé."""
    org, branch, service = await _setup_branch(db_session)

    with pytest.raises(HTTPException) as exc_info:
        await ticket_service.create_ticket(
            _request(branch.id, service.id, "emergency"),
            user_id="client-priority-1",
            org_id=org.id,
            actor_role=UserRole.CLIENT,
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "PRIORITY_FORBIDDEN"
    assert "standard" in exc_info.value.detail["allowed_priorities"]

    # Aucun ticket ne doit avoir été persisté
    from sqlalchemy import select, func
    count = await db_session.execute(
        select(func.count(Ticket.id)).where(Ticket.user_id == "client-priority-1")
    )
    assert count.scalar_one() == 0


@pytest.mark.asyncio
async def test_client_cannot_self_assign_vip(ticket_service, db_session, mock_queue):
    """Un client qui demande 'vip' est refusé (403)."""
    org, branch, service = await _setup_branch(db_session)

    with pytest.raises(HTTPException) as exc_info:
        await ticket_service.create_ticket(
            _request(branch.id, service.id, "vip"),
            user_id="client-priority-2",
            org_id=org.id,
            actor_role=UserRole.CLIENT,
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "PRIORITY_FORBIDDEN"


@pytest.mark.asyncio
async def test_unknown_actor_role_defaults_to_client(ticket_service, db_session, mock_queue):
    """actor_role=None (appelant non identifié) est traité comme CLIENT — refus par défaut."""
    org, branch, service = await _setup_branch(db_session)

    with pytest.raises(HTTPException) as exc_info:
        await ticket_service.create_ticket(
            _request(branch.id, service.id, "emergency"),
            user_id="client-priority-3",
            org_id=org.id,
            actor_role=None,
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_client_can_request_priority(ticket_service, db_session, mock_queue):
    """La priorité 'priority' (femme enceinte, personne âgée…) reste auto-déclarable."""
    org, branch, service = await _setup_branch(db_session)

    result = await ticket_service.create_ticket(
        _request(branch.id, service.id, "priority"),
        user_id="client-priority-4",
        org_id=org.id,
        actor_role=UserRole.CLIENT,
    )

    assert result.priority == TicketPriority.PRIORITY.value
    assert result.status == "waiting"


@pytest.mark.asyncio
async def test_staff_can_assign_emergency(ticket_service, db_session, mock_queue):
    """Un agent (staff) peut attribuer 'emergency' — usage guichet."""
    org, branch, service = await _setup_branch(db_session)

    result = await ticket_service.create_ticket(
        _request(branch.id, service.id, "emergency"),
        user_id="staff-priority-1",
        org_id=org.id,
        actor_role=UserRole.AGENT,
    )

    assert result.priority == TicketPriority.EMERGENCY.value


@pytest.mark.asyncio
async def test_invalid_priority_raises_400(ticket_service, db_session, mock_queue):
    """Une priorité inconnue renvoie 400 INVALID_PRIORITY (plus de repli silencieux)."""
    org, branch, service = await _setup_branch(db_session)

    # Contourne la validation Pydantic pour tester le garde-fou du service
    with pytest.raises(HTTPException) as exc_info:
        await ticket_service._resolve_priority("turbo", UserRole.CLIENT)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "INVALID_PRIORITY"
