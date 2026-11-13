"""
Tests d'isolation multi-tenant — Phase 3

Vérifie qu'un utilisateur d'org A ne peut jamais voir, modifier
ou interagir avec les données d'org B.

Règle absolue TONDE : chaque requête DB filtre par org_id.
Ces tests constituent la ligne de défense contre les fuites de données.
"""
import pytest
from fastapi import HTTPException
from unittest.mock import patch, AsyncMock

from app.services.ticket_service import TicketService
from app.services.branch_service import BranchService
from app.services.organization_service import OrganizationService
from app.schemas.ticket import CreateTicketRequest
from app.schemas.organization import CreateOrganizationRequest
from app.schemas.branch import CreateBranchRequest
from app.models.user import User
from app.models.ticket import TicketStatus


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _create_org(db, slug: str, name: str) -> str:
    svc = OrganizationService(db)
    org = await svc.create(CreateOrganizationRequest(name=name, slug=slug, sector="bank"))
    return org.id


async def _create_agency(db, org_id: str, slug: str) -> str:
    svc = BranchService(db)
    agency = await svc.create(
        org_id,
        CreateBranchRequest(name=f"Agence {slug}", slug=slug, sector="bank"),
        caller_org_id=None,
    )
    # Ouvrir la branch pour permettre la création de tickets dans les tests
    from app.models.branch import Branch
    from sqlalchemy import select as sa_select
    result = await db.execute(sa_select(Branch).where(Branch.id == agency.id))
    ag = result.scalar_one()
    ag.is_open = True
    ag.is_active = True
    await db.commit()
    return agency.id


async def _create_user(db, email: str, org_id: str) -> User:
    user = User(email=email, name="Test User", org_id=org_id, is_active=True, is_verified=True)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _create_service_entity(db, agency_id: str, org_id: str) -> str:
    """Crée un Service dans une branch et retourne son ID."""
    from app.models.branch import Service
    svc = Service(
        branch_id=agency_id,
        org_id=org_id,
        name="Service Test",
        ticket_prefix="T",
        avg_duration_minutes=5,
        is_active=True,
    )
    db.add(svc)
    await db.commit()
    await db.refresh(svc)
    return svc.id


@pytest.fixture
def mock_queue():
    """Mock Redis pour les opérations de file d'attente."""
    with patch("app.services.ticket_service.add_to_queue", new_callable=AsyncMock, return_value=1):
        with patch("app.services.ticket_service.get_queue_size", new_callable=AsyncMock, return_value=1):
            with patch("app.services.ticket_service.remove_from_queue", new_callable=AsyncMock):
                with patch("app.services.ticket_service.pop_next_ticket", new_callable=AsyncMock):
                    with patch("app.services.ticket_service.get_queue_position", new_callable=AsyncMock, return_value=1):
                        yield


# ── Tests isolation Tickets ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_user_org_a_cannot_see_ticket_of_org_b(db_session, mock_redis, mock_queue):
    """Un user d'org A ne peut pas récupérer un ticket appartenant à org B."""
    org_a = await _create_org(db_session, "org-mt-a", "Org MT A")
    org_b = await _create_org(db_session, "org-mt-b", "Org MT B")

    agency_b = await _create_agency(db_session, org_b, "agency-mt-b")
    service_b = await _create_service_entity(db_session, agency_b, org_b)

    user_b = await _create_user(db_session, "user_b@test.bi", org_b)
    user_a = await _create_user(db_session, "user_a@test.bi", org_a)

    svc_b = TicketService(db_session)
    ticket = await svc_b.create_ticket(
        CreateTicketRequest(agency_id=agency_b, service_id=service_b),
        user_id=user_b.id,
        org_id=org_b,
    )

    # L'user d'org A tente de récupérer un ticket d'org B
    svc_a = TicketService(db_session)
    with pytest.raises(HTTPException) as exc_info:
        await svc_a.get_ticket(ticket.id, user_a.id, org_a)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["code"] == "TICKET_NOT_FOUND"


@pytest.mark.asyncio
async def test_user_org_a_cannot_cancel_ticket_of_org_b(db_session, mock_redis, mock_queue):
    """Un user d'org A ne peut pas annuler un ticket appartenant à org B."""
    org_a = await _create_org(db_session, "org-cancel-a", "Org Cancel A")
    org_b = await _create_org(db_session, "org-cancel-b", "Org Cancel B")

    agency_b = await _create_agency(db_session, org_b, "agency-cancel-b")
    service_b = await _create_service_entity(db_session, agency_b, org_b)

    user_b = await _create_user(db_session, "cancel_b@test.bi", org_b)
    user_a = await _create_user(db_session, "cancel_a@test.bi", org_a)

    svc = TicketService(db_session)
    ticket = await svc.create_ticket(
        CreateTicketRequest(agency_id=agency_b, service_id=service_b),
        user_id=user_b.id,
        org_id=org_b,
    )

    with pytest.raises(HTTPException) as exc_info:
        await svc.cancel_ticket(ticket.id, user_a.id, org_a)

    assert exc_info.value.status_code in (403, 404)


@pytest.mark.asyncio
async def test_user_org_a_ticket_not_visible_in_org_b_history(db_session, mock_redis, mock_queue):
    """L'historique de tickets d'org A ne contient pas les tickets d'org B."""
    org_a = await _create_org(db_session, "org-hist-a", "Org Hist A")
    org_b = await _create_org(db_session, "org-hist-b", "Org Hist B")

    agency_a = await _create_agency(db_session, org_a, "agency-hist-a")
    agency_b = await _create_agency(db_session, org_b, "agency-hist-b")
    service_a = await _create_service_entity(db_session, agency_a, org_a)
    service_b = await _create_service_entity(db_session, agency_b, org_b)

    user_a = await _create_user(db_session, "hist_a@test.bi", org_a)
    user_b = await _create_user(db_session, "hist_b@test.bi", org_b)

    svc_a = TicketService(db_session)
    svc_b = TicketService(db_session)

    await svc_a.create_ticket(
        CreateTicketRequest(agency_id=agency_a, service_id=service_a),
        user_id=user_a.id,
        org_id=org_a,
    )
    await svc_b.create_ticket(
        CreateTicketRequest(agency_id=agency_b, service_id=service_b),
        user_id=user_b.id,
        org_id=org_b,
    )

    # L'historique de user_a doit contenir exactement 1 ticket (le sien)
    # et pas le ticket de user_b (même si get_history ne filtre pas par org,
    # il filtre par user_id — ce qui garantit l'isolation)
    history_a = await svc_a.get_history(user_a.id)
    assert history_a.total == 1

    # L'historique de user_b aussi doit contenir exactement 1 ticket
    history_b = await svc_b.get_history(user_b.id)
    assert history_b.total == 1

    # Les deux historiques ne se chevauchent pas
    assert history_a.items[0].id != history_b.items[0].id


# ── Tests isolation Agencies ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_org_a_cannot_see_agencies_of_org_b(db_session):
    """list_agencies() d'org A ne retourne pas les agences d'org B."""
    org_a = await _create_org(db_session, "org-ag-a", "Org Ag A")
    org_b = await _create_org(db_session, "org-ag-b", "Org Ag B")

    await _create_agency(db_session, org_a, "ag-a-1")
    await _create_agency(db_session, org_a, "ag-a-2")
    await _create_agency(db_session, org_b, "ag-b-1")

    svc = BranchService(db_session)
    result = await svc.list(caller_org_id=org_a, org_id=org_a)

    agency_org_ids = {a.org_id for a in result.items}
    assert org_b not in agency_org_ids
    assert result.total == 2


@pytest.mark.asyncio
async def test_org_a_cannot_update_agency_of_org_b(db_session):
    """Un admin d'org A ne peut pas modifier une branch d'org B."""
    from app.schemas.branch import UpdateBranchRequest

    org_a = await _create_org(db_session, "org-upd-a", "Org Upd A")
    org_b = await _create_org(db_session, "org-upd-b", "Org Upd B")

    agency_b_id = await _create_agency(db_session, org_b, "ag-upd-b")

    svc = BranchService(db_session)
    with pytest.raises(HTTPException) as exc_info:
        await svc.update(
            agency_b_id,
            UpdateBranchRequest(name="Hack"),
            caller_org_id=org_a,
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["code"] == "BRANCH_NOT_FOUND"

# ── Tests isolation global : org_id dans le JWT ───────────────────────────────

@pytest.mark.asyncio
async def test_ticket_create_uses_org_id_from_jwt_not_client(db_session, mock_redis, mock_queue):
    """
    L'org_id utilisé pour créer un ticket vient du JWT (paramètre org_id),
    jamais du body de la requête.

    Simule un attaquant qui tente de créer un ticket dans org B
    en étant authentifié dans org A.
    """
    org_a = await _create_org(db_session, "org-jwt-a", "Org JWT A")
    org_b = await _create_org(db_session, "org-jwt-b", "Org JWT B")

    # L'agence appartient à org B
    agency_b = await _create_agency(db_session, org_b, "agency-jwt-b")
    service_b = await _create_service_entity(db_session, agency_b, org_b)

    user_a = await _create_user(db_session, "jwt_a@test.bi", org_a)

    svc = TicketService(db_session)
    # Le user est authentifié dans org_a, mais tente de créer un ticket dans org_b
    # L'agence n'appartient pas à org_a → doit être refusé
    with pytest.raises(HTTPException) as exc_info:
        await svc.create_ticket(
            CreateTicketRequest(agency_id=agency_b, service_id=service_b),
            user_id=user_a.id,
            org_id=org_a,  # org du JWT — pas celui de l'agence
        )

    # L'agence n'est pas visible depuis org_a
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["code"] in ("BRANCH_NOT_FOUND", "SERVICE_NOT_FOUND")
