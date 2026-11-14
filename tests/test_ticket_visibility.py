"""
Tests de régression — visibilité et cycle de vie des tickets.

Couvre les défauts corrigés sur le chemin de LECTURE d'un ticket :

1. `get_ticket()` appliquait le contrôle de propriété à TOUS les rôles. Un
   guichetier ne pouvait donc pas ouvrir le ticket qu'il venait d'appeler
   (403 systématique), ce qui cassait tout l'enchaînement /serving → /done.

2. Le routage se faisait sur `org_id is None` et non sur le rôle. Or un
   SUPER_ADMIN a `org_id = None` : il tombait dans le chemin CLIENT et
   recevait 404 sur un ticket parfaitement valide.

3. Aucun contrôle de périmètre branch : un agent de l'agence A lisait les
   tickets de l'agence B de la même organisation, alors que tous les chemins
   d'écriture appliquent déjà ce périmètre.

4. Lockout : ABSENT doit pouvoir être annulé, sinon un client qui rate son
   tour reste bloqué par la règle « un seul ticket actif ».
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.branch import Branch, Service
from app.models.employee import Employee, EmployeeStatus
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


# ── Construction du monde de test ─────────────────────────────────────────────

async def _make_org(db_session, slug: str = "hopital-test") -> Organization:
    org = Organization(name="Hopital Test", slug=slug, sector="hospital")
    db_session.add(org)
    await db_session.commit()
    await db_session.refresh(org)
    return org


async def _make_branch(db_session, org: Organization, name: str = "Accueil") -> Branch:
    branch = Branch(
        org_id=org.id,
        name=name,
        slug=f"{name.lower()}-{org.id[:8]}",
        sector="hospital",
        is_active=True,
        is_open=True,
    )
    db_session.add(branch)
    await db_session.commit()
    await db_session.refresh(branch)
    return branch


async def _make_service(db_session, org: Organization, branch: Branch, prefix: str = "A") -> Service:
    service = Service(
        org_id=org.id,
        branch_id=branch.id,
        name="Consultation",
        ticket_prefix=prefix,
        avg_duration_minutes=5,
    )
    db_session.add(service)
    await db_session.commit()
    await db_session.refresh(service)
    return service


async def _make_user(
    db_session,
    role: UserRole = UserRole.CLIENT,
    org_id: str | None = None,
    phone: str = "+25779000111",
) -> User:
    user = User(phone=phone, is_active=True, role=role, org_id=org_id)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _make_staff(db_session, org: Organization, branch: Branch, role: UserRole, phone: str) -> User:
    """Employé ACTIF rattaché à une branch : c'est ce rattachement qui définit son périmètre."""
    user = await _make_user(db_session, role=role, org_id=org.id, phone=phone)
    employee = Employee(
        org_id=org.id,
        user_id=user.id,
        agency_id=branch.id,
        role=role,
        status=EmployeeStatus.ACTIVE,
    )
    db_session.add(employee)
    await db_session.commit()
    return user


async def _create(ticket_service: TicketService, branch, service, user_id, org_id):
    return await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=branch.id, service_id=service.id),
        user_id=user_id,
        org_id=org_id,
    )


# ── 1 & 3. Visibilité staff, périmètre branch ────────────────────────────────

@pytest.mark.asyncio
async def test_staff_can_read_client_ticket(ticket_service, db_session, mock_queue):
    """Un agent de la même branch consulte le ticket d'un client."""
    org = await _make_org(db_session)
    branch = await _make_branch(db_session, org)
    service = await _make_service(db_session, org, branch)
    client = await _make_user(db_session, phone="+25779000111")
    agent = await _make_staff(db_session, org, branch, UserRole.AGENT, "+25779000222")

    ticket = await _create(ticket_service, branch, service, client.id, org.id)

    result = await ticket_service.get_ticket(ticket.id, agent)

    assert result.id == ticket.id
    assert result.number == ticket.number


@pytest.mark.asyncio
async def test_agent_from_another_branch_gets_403(ticket_service, db_session, mock_queue):
    """
    Un agent de l'agence A ne lit pas un ticket de l'agence B de la même org.

    Le périmètre branch est appliqué sur tous les chemins d'écriture ; il doit
    l'être aussi en lecture, sinon un guichetier voit la file d'une autre agence.
    """
    org = await _make_org(db_session)
    branch_a = await _make_branch(db_session, org, "Agence A")
    branch_b = await _make_branch(db_session, org, "Agence B")
    service_b = await _make_service(db_session, org, branch_b, prefix="B")
    client = await _make_user(db_session, phone="+25779000333")
    agent_a = await _make_staff(db_session, org, branch_a, UserRole.AGENT, "+25779000444")

    ticket_b = await _create(ticket_service, branch_b, service_b, client.id, org.id)

    with pytest.raises(HTTPException) as exc_info:
        await ticket_service.get_ticket(ticket_b.id, agent_a)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "BRANCH_FORBIDDEN"


@pytest.mark.asyncio
async def test_admin_org_reads_any_branch_of_his_org(ticket_service, db_session, mock_queue):
    """ADMIN_ORG est transverse : il lit toutes les branches de son organisation."""
    org = await _make_org(db_session)
    branch_a = await _make_branch(db_session, org, "Agence A")
    branch_b = await _make_branch(db_session, org, "Agence B")
    service_b = await _make_service(db_session, org, branch_b, prefix="B")
    client = await _make_user(db_session, phone="+25779000555")
    admin = await _make_staff(db_session, org, branch_a, UserRole.ADMIN_ORG, "+25779000666")

    ticket_b = await _create(ticket_service, branch_b, service_b, client.id, org.id)

    result = await ticket_service.get_ticket(ticket_b.id, admin)
    assert result.id == ticket_b.id


@pytest.mark.asyncio
async def test_super_admin_reads_ticket_without_org(ticket_service, db_session, mock_queue):
    """
    SUPER_ADMIN n'a aucune organisation (org_id = None).

    Ancien défaut : le routage se faisait sur `org_id is None`, donc le
    super-admin suivait le chemin CLIENT et recevait 404 sur un ticket valide.
    """
    org = await _make_org(db_session)
    branch = await _make_branch(db_session, org)
    service = await _make_service(db_session, org, branch)
    client = await _make_user(db_session, phone="+25779000777")
    super_admin = await _make_user(
        db_session, role=UserRole.SUPER_ADMIN, org_id=None, phone="+25779000888"
    )

    ticket = await _create(ticket_service, branch, service, client.id, org.id)

    assert super_admin.org_id is None
    result = await ticket_service.get_ticket(ticket.id, super_admin)
    assert result.id == ticket.id


# ── 2. Isolation client ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_client_cannot_read_other_client_ticket(ticket_service, db_session, mock_queue):
    """Un client de la même organisation ne lit pas le ticket d'un autre (403)."""
    org = await _make_org(db_session)
    branch = await _make_branch(db_session, org)
    service = await _make_service(db_session, org, branch)
    owner = await _make_user(db_session, org_id=org.id, phone="+25779001001")
    intruder = await _make_user(db_session, org_id=org.id, phone="+25779001002")

    ticket = await _create(ticket_service, branch, service, owner.id, org.id)

    with pytest.raises(HTTPException) as exc_info:
        await ticket_service.get_ticket(ticket.id, intruder)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_org_less_client_cannot_read_other_clients_ticket(ticket_service, db_session, mock_queue):
    """Client OTP sans organisation : 404, jamais 403 (ne révèle pas l'existence)."""
    org = await _make_org(db_session)
    branch = await _make_branch(db_session, org)
    service = await _make_service(db_session, org, branch)
    owner = await _make_user(db_session, phone="+25779001003")
    intruder = await _make_user(db_session, phone="+25779001004")

    ticket = await _create(ticket_service, branch, service, owner.id, org.id)

    with pytest.raises(HTTPException) as exc_info:
        await ticket_service.get_ticket(ticket.id, intruder)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_staff_cannot_read_ticket_from_another_org(ticket_service, db_session, mock_queue):
    """Le staff reste cloisonné à son organisation (404, pas de fuite)."""
    org = await _make_org(db_session, "org-a")
    other_org = await _make_org(db_session, "org-b")
    branch = await _make_branch(db_session, org)
    other_branch = await _make_branch(db_session, other_org, "Agence B")
    service = await _make_service(db_session, org, branch)
    client = await _make_user(db_session, phone="+25779001005")
    agent_other = await _make_staff(
        db_session, other_org, other_branch, UserRole.AGENT, "+25779001006"
    )

    ticket = await _create(ticket_service, branch, service, client.id, org.id)

    with pytest.raises(HTTPException) as exc_info:
        await ticket_service.get_ticket(ticket.id, agent_other)

    assert exc_info.value.status_code == 404


# ── 4. Lockout ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_absent_ticket_can_be_cancelled(ticket_service, db_session, mock_queue):
    """ABSENT → CANCELLED est autorisé : le client n'est plus prisonnier."""
    org = await _make_org(db_session)
    branch = await _make_branch(db_session, org)
    service = await _make_service(db_session, org, branch)
    client = await _make_user(db_session, phone="+25779001007")

    ticket = await _create(ticket_service, branch, service, client.id, org.id)

    # Un agent a marqué le client absent (l'affectation guichet est vérifiée
    # ailleurs) : on force l'état pour isoler la transition qui nous intéresse.
    row = (await db_session.execute(select(Ticket).where(Ticket.id == ticket.id))).scalar_one()
    row.status = TicketStatus.ABSENT
    await db_session.commit()

    await ticket_service.cancel_ticket(ticket.id, client.id, org.id)

    final = (await db_session.execute(select(Ticket).where(Ticket.id == ticket.id))).scalar_one()
    assert final.status == TicketStatus.CANCELLED


# ── 5. Super-admin sur les branches ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_super_admin_can_read_branch(db_session):
    """
    Un SUPER_ADMIN n'a pas d'org_id : BranchService doit interpréter None
    comme « pas de filtre » et non comme « org inconnue ».
    """
    org = await _make_org(db_session)
    branch = await _make_branch(db_session, org)

    service = BranchService(db_session)
    result = await service.get_by_id(branch.id, caller_org_id=None)

    assert result.id == branch.id


# ── 6. Horodatages du cycle de vie ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_ticket_response_exposes_lifecycle_timestamps(ticket_service, db_session, mock_queue):
    """
    called_at / served_at / done_at doivent sortir dans la réponse.

    Sans eux, le poste guichet déduisait l'heure d'appel de l'instant du fetch :
    après une reprise ou une reconnexion, la durée de service affichée était
    fausse (elle repartait de zéro).
    """
    from datetime import datetime, timedelta, timezone

    org = await _make_org(db_session)
    branch = await _make_branch(db_session, org)
    service = await _make_service(db_session, org, branch)
    client = await _make_user(db_session, phone="+25779003001")

    ticket = await _create(ticket_service, branch, service, client.id, org.id)

    # Ticket neuf : aucun horodatage de cycle de vie.
    fresh = await ticket_service.get_ticket(ticket.id, client)
    assert fresh.called_at is None
    assert fresh.served_at is None
    assert fresh.done_at is None

    # Ticket appelé puis servi : les trois champs remontent tels quels.
    now = datetime.now(timezone.utc)
    row = (await db_session.execute(select(Ticket).where(Ticket.id == ticket.id))).scalar_one()
    row.status = TicketStatus.SERVING
    row.called_at = now - timedelta(minutes=4)
    row.served_at = now - timedelta(minutes=3)
    await db_session.commit()

    view = await ticket_service.get_ticket(ticket.id, client)
    assert view.called_at is not None
    assert view.served_at is not None
    assert view.done_at is None
    assert view.called_at < view.served_at
