"""
Tests unitaires — TicketService (Queue Engine)

Couvre :
  - Création de ticket : cas nominal
  - Création : agence fermée → 400
  - Création : ticket actif existant → 409
  - Annulation : cas nominal
  - Annulation : ticket d'un autre user → 403
  - Machine à états : transitions autorisées
  - Machine à états : transitions interdites → 400
"""
import pytest
from unittest.mock import patch, AsyncMock
from fastapi import HTTPException

from app.models.organization import Organization
from app.models.branch import Branch, Service
from app.models.ticket import TicketStatus, TicketPriority
from app.schemas.ticket import CreateTicketRequest


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_queue(mock_redis):
    """Mock des helpers Redis de file d'attente — signatures avec service_id (TASK-04)."""
    async def _fake_next_sequence(agency_id, prefix, day, db_max):
        """Reproduit le démarrage à froid du compteur : MAX(sequence) + 1."""
        return db_max + 1

    with patch("app.services.ticket_service.next_daily_sequence", side_effect=_fake_next_sequence):
        with patch("app.services.ticket_service.add_to_queue", new_callable=AsyncMock, return_value=1):
            with patch("app.services.ticket_service.get_queue_size", new_callable=AsyncMock, return_value=1):
                with patch("app.services.ticket_service.remove_from_queue", new_callable=AsyncMock):
                    with patch("app.services.ticket_service.get_next_ticket", new_callable=AsyncMock):
                        with patch("app.services.ticket_service.get_queue_position", new_callable=AsyncMock, return_value=1):
                            yield


async def _create_test_org(db_session) -> Organization:
    """Crée une organisation de test."""
    org = Organization(
        name="Banque Test Burundi",
        slug="banque-test-bi",
        sector="bank",
        country="Burundi",
    )
    db_session.add(org)
    await db_session.commit()
    await db_session.refresh(org)
    return org


async def _create_test_agency(db_session, org_id: str, is_open: bool = True) -> Branch:
    """Crée une branch de test."""
    branch = Branch(
        org_id=org_id,
        name="Agence Centre-ville",
        slug=f"agence-centre-{org_id[:8]}",
        sector="bank",
        is_active=True,
        is_open=is_open,
    )
    db_session.add(branch)
    await db_session.commit()
    await db_session.refresh(branch)
    return branch


async def _create_test_service(db_session, agency_id: str, org_id: str) -> Service:
    """Crée un service de test."""
    service = Service(
        org_id=org_id,
        branch_id=agency_id,
        name="Dépôt",
        ticket_prefix="A",
        avg_duration_minutes=5,
    )
    db_session.add(service)
    await db_session.commit()
    await db_session.refresh(service)
    return service


# ── Tests création ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_ticket_success(ticket_service, db_session, mock_queue):
    """Création de ticket dans le cas nominal."""
    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)

    result = await ticket_service.create_ticket(
        CreateTicketRequest(
            agency_id=agency.id,
            service_id=service.id,
            priority="standard",
        ),
        user_id="user-test-123",
        org_id=org.id,
    )

    assert result.number == "A-1"
    assert result.status == "waiting"
    assert result.priority == "standard"
    assert result.position == 1


@pytest.mark.asyncio
async def test_create_ticket_agency_closed_raises_400(ticket_service, db_session, mock_queue):
    """Agence fermée → HTTP 400."""
    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id, is_open=False)
    service = await _create_test_service(db_session, agency.id, org.id)

    with pytest.raises(HTTPException) as exc_info:
        await ticket_service.create_ticket(
            CreateTicketRequest(
                agency_id=agency.id,
                service_id=service.id,
            ),
            user_id="user-test-123",
            org_id=org.id,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "BRANCH_CLOSED"


@pytest.mark.asyncio
async def test_create_ticket_duplicate_raises_409(ticket_service, db_session, mock_queue):
    """Un ticket actif existe déjà → HTTP 409 avec code TICKET_ALREADY_ACTIVE et active_ticket_id."""
    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)
    user_id = "user-dup-test"

    # Premier ticket
    first = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency.id, service_id=service.id),
        user_id=user_id,
        org_id=org.id,
    )

    # Deuxième ticket → conflit
    with pytest.raises(HTTPException) as exc_info:
        await ticket_service.create_ticket(
            CreateTicketRequest(agency_id=agency.id, service_id=service.id),
            user_id=user_id,
            org_id=org.id,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "TICKET_ALREADY_ACTIVE"
    # Le mobile peut rediriger vers le ticket actif grâce à ces champs
    assert exc_info.value.detail["active_ticket_id"] == first.id
    assert exc_info.value.detail["active_ticket_number"] == first.number


@pytest.mark.asyncio
async def test_create_ticket_wrong_org_raises_404(ticket_service, db_session, mock_queue):
    """Agence d'une autre organisation → 404 (isolation multi-tenant)."""
    org_a = await _create_test_org(db_session)
    # org_b simule une autre organisation
    org_b = Organization(name="Org B", slug="org-b", sector="hospital")
    db_session.add(org_b)
    await db_session.commit()
    await db_session.refresh(org_b)

    agency = await _create_test_agency(db_session, org_a.id)
    service = await _create_test_service(db_session, agency.id, org_a.id)

    with pytest.raises(HTTPException) as exc_info:
        await ticket_service.create_ticket(
            CreateTicketRequest(agency_id=agency.id, service_id=service.id),
            user_id="user-test",
            org_id=org_b.id,  # ← mauvaise org
        )

    assert exc_info.value.status_code == 404


# ── Tests annulation ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_ticket_success(ticket_service, db_session, mock_queue):
    """Annulation d'un ticket WAITING réussit."""
    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)
    user_id = "user-cancel-test"

    ticket = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency.id, service_id=service.id),
        user_id=user_id,
        org_id=org.id,
    )

    result = await ticket_service.cancel_ticket(ticket.id, user_id, org.id)
    assert result["success"] is True


@pytest.mark.asyncio
async def test_cancel_ticket_wrong_user_raises_403(ticket_service, db_session, mock_queue):
    """Annulation par un autre utilisateur → HTTP 403."""
    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)

    ticket = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency.id, service_id=service.id),
        user_id="owner-user",
        org_id=org.id,
    )

    with pytest.raises(HTTPException) as exc_info:
        await ticket_service.cancel_ticket(ticket.id, "other-user", org.id)

    assert exc_info.value.status_code == 403


# ── Tests machine à états ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_state_machine_waiting_to_called(ticket_service, db_session, mock_queue):
    """Transition WAITING → CALLED autorisée."""
    from app.models.ticket import Ticket

    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)

    ticket_resp = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency.id, service_id=service.id),
        user_id="user-fsm",
        org_id=org.id,
    )

    # Récupérer le vrai objet Ticket
    from sqlalchemy import select
    result = await db_session.execute(
        select(Ticket).where(Ticket.id == ticket_resp.id)
    )
    ticket = result.scalar_one()

    # La transition doit réussir sans exception
    await ticket_service._transition(ticket, TicketStatus.CALLED)
    assert ticket.status == TicketStatus.CALLED


@pytest.mark.asyncio
async def test_state_machine_invalid_transition_raises_400(ticket_service, db_session, mock_queue):
    """Transition interdite → HTTP 400 avec message explicite."""
    from app.models.ticket import Ticket
    from sqlalchemy import select

    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)

    ticket_resp = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency.id, service_id=service.id),
        user_id="user-fsm-invalid",
        org_id=org.id,
    )

    result = await db_session.execute(
        select(Ticket).where(Ticket.id == ticket_resp.id)
    )
    ticket = result.scalar_one()

    # WAITING → DONE est interdit (doit passer par CALLED → SERVING → DONE)
    with pytest.raises(HTTPException) as exc_info:
        await ticket_service._transition(ticket, TicketStatus.DONE)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "INVALID_TRANSITION"


@pytest.mark.asyncio
async def test_state_machine_terminal_states_are_blocked(ticket_service, db_session, mock_queue):
    """Les états terminaux (DONE, CANCELLED) n'autorisent aucune transition."""
    from app.models.ticket import Ticket, ALLOWED_TRANSITIONS

    terminal_states = [
        TicketStatus.DONE,
        TicketStatus.CANCELLED,
        TicketStatus.INCOMPLETE,
        TicketStatus.TRANSFERRED,
    ]

    for state in terminal_states:
        assert ALLOWED_TRANSITIONS[state] == [], \
            f"L'état {state} devrait être terminal (aucune transition autorisée)"


# ── TASK-03 : tests règle "1 ticket actif global" ─────────────────────────────

@pytest.mark.asyncio
async def test_active_statuses_constant():
    """
    ACTIVE_STATUSES contient exactement les 4 statuts dont on peut sortir.

    TRANSFERRED et INCOMPLETE en sont volontairement exclus : ce sont des états
    terminaux (ALLOWED_TRANSITIONS = []). Les y laisser bloquait le client à vie
    après un transfert ou un service interrompu.
    """
    from app.services.ticket_service import ACTIVE_STATUSES

    expected = {
        TicketStatus.WAITING,
        TicketStatus.CALLED,
        TicketStatus.SERVING,
        TicketStatus.ABSENT,
    }
    assert set(ACTIVE_STATUSES) == expected


def test_terminal_states_never_block_new_ticket():
    """Aucun état terminal ne doit figurer dans ACTIVE_STATUSES."""
    from app.services.ticket_service import ACTIVE_STATUSES
    from app.models.ticket import ALLOWED_TRANSITIONS

    terminal = {s for s, allowed in ALLOWED_TRANSITIONS.items() if not allowed}
    assert terminal & set(ACTIVE_STATUSES) == set(), (
        "Un état terminal dans ACTIVE_STATUSES bloque définitivement le client"
    )


@pytest.mark.asyncio
async def test_one_active_ticket_global_rule(ticket_service, db_session, mock_queue):
    """Un user avec ticket WAITING dans agence A ne peut pas prendre un ticket dans agence B."""
    org = await _create_test_org(db_session)
    agency_a = await _create_test_agency(db_session, org.id)
    agency_b = Branch(
        org_id=org.id,
        name="Agence B",
        slug=f"agence-b-{org.id[:8]}",
        sector="bank",
        is_active=True,
        is_open=True,
    )
    db_session.add(agency_b)
    await db_session.commit()
    await db_session.refresh(agency_b)

    service_a = await _create_test_service(db_session, agency_a.id, org.id)
    service_b = await _create_test_service(db_session, agency_b.id, org.id)

    user_id = "user-global-rule"

    # Premier ticket dans agence A
    first = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency_a.id, service_id=service_a.id),
        user_id=user_id,
        org_id=org.id,
    )
    assert first.status == "waiting"

    # Deuxième ticket dans agence B → doit être bloqué globalement
    with pytest.raises(HTTPException) as exc_info:
        await ticket_service.create_ticket(
            CreateTicketRequest(agency_id=agency_b.id, service_id=service_b.id),
            user_id=user_id,
            org_id=org.id,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "TICKET_ALREADY_ACTIVE"


@pytest.mark.asyncio
async def test_ticket_allowed_after_cancel(ticket_service, db_session, mock_queue):
    """Après annulation (CANCELLED), l'user peut prendre un nouveau ticket."""
    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)
    user_id = "user-after-cancel"

    # Premier ticket puis annulation
    first = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency.id, service_id=service.id),
        user_id=user_id,
        org_id=org.id,
    )
    await ticket_service.cancel_ticket(first.id, user_id, org.id)

    # Nouveau ticket doit réussir
    second = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency.id, service_id=service.id),
        user_id=user_id,
        org_id=org.id,
    )
    assert second.status == "waiting"


@pytest.mark.asyncio
async def test_ticket_allowed_after_done(ticket_service, db_session, mock_queue):
    """Après DONE, l'user peut prendre un nouveau ticket."""
    from app.models.ticket import Ticket
    from sqlalchemy import select as sa_select

    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)
    user_id = "user-after-done"

    first_resp = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency.id, service_id=service.id),
        user_id=user_id,
        org_id=org.id,
    )

    # Faire passer le ticket jusqu'à DONE via la machine à états
    result = await db_session.execute(sa_select(Ticket).where(Ticket.id == first_resp.id))
    ticket = result.scalar_one()
    await ticket_service._transition(ticket, TicketStatus.CALLED)
    await ticket_service._transition(ticket, TicketStatus.SERVING)
    await ticket_service._transition(ticket, TicketStatus.DONE)
    await db_session.commit()

    # Nouveau ticket doit réussir
    second = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency.id, service_id=service.id),
        user_id=user_id,
        org_id=org.id,
    )
    assert second.status == "waiting"


@pytest.mark.asyncio
async def test_absent_ticket_blocks_new_ticket(ticket_service, db_session, mock_queue):
    """Un ticket en statut ABSENT bloque la création d'un nouveau ticket."""
    from app.models.ticket import Ticket
    from sqlalchemy import select as sa_select

    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)
    user_id = "user-absent-test"

    first_resp = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency.id, service_id=service.id),
        user_id=user_id,
        org_id=org.id,
    )

    # Faire passer le ticket en ABSENT
    result = await db_session.execute(sa_select(Ticket).where(Ticket.id == first_resp.id))
    ticket = result.scalar_one()
    await ticket_service._transition(ticket, TicketStatus.CALLED)
    await ticket_service._transition(ticket, TicketStatus.ABSENT)
    await db_session.commit()

    # Nouveau ticket doit être bloqué (ABSENT est dans ACTIVE_STATUSES)
    with pytest.raises(HTTPException) as exc_info:
        await ticket_service.create_ticket(
            CreateTicketRequest(agency_id=agency.id, service_id=service.id),
            user_id=user_id,
            org_id=org.id,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "TICKET_ALREADY_ACTIVE"


@pytest.mark.asyncio
async def test_409_includes_active_ticket_id(ticket_service, db_session, mock_queue):
    """La réponse 409 contient active_ticket_id et active_ticket_number pour la redirection mobile."""
    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)
    user_id = "user-409-fields"

    first = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency.id, service_id=service.id),
        user_id=user_id,
        org_id=org.id,
    )

    with pytest.raises(HTTPException) as exc_info:
        await ticket_service.create_ticket(
            CreateTicketRequest(agency_id=agency.id, service_id=service.id),
            user_id=user_id,
            org_id=org.id,
        )

    detail = exc_info.value.detail
    assert detail["code"] == "TICKET_ALREADY_ACTIVE"
    assert detail["active_ticket_id"] == first.id
    assert detail["active_ticket_number"] == first.number
    assert "message" in detail


# ── TASK-04 : tests clés Redis segmentées par service ─────────────────────────

def test_queue_key_includes_service_id():
    """_queue_key() produit exactement tonde:{org}:{agency}:{service}:queue."""
    from app.core.redis import _queue_key

    key = _queue_key("org-1", "agency-1", "service-1")
    assert key == "tonde:org-1:agency-1:service-1:queue"


def test_two_services_have_independent_queues():
    """Deux service_id différents produisent des clés Redis distinctes."""
    from app.core.redis import _queue_key

    key_a = _queue_key("org-1", "agency-1", "caisse")
    key_b = _queue_key("org-1", "agency-1", "credit")

    assert key_a != key_b
    assert "caisse" in key_a
    assert "credit" in key_b


def test_queue_key_format_with_various_ids():
    """_queue_key() est déterministe et inclut les 3 segments."""
    from app.core.redis import _queue_key

    org = "org-abc"
    agency = "agency-xyz"
    service = "service-123"

    key = _queue_key(org, agency, service)

    assert key.startswith("tonde:")
    assert key.endswith(":queue")
    assert org in key
    assert agency in key
    assert service in key


@pytest.mark.asyncio
async def test_create_ticket_passes_service_id_to_redis(ticket_service, db_session, mock_redis):
    """create_ticket() passe ticket.service_id à add_to_queue()."""
    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)

    with patch("app.services.ticket_service.add_to_queue", new_callable=AsyncMock, return_value=1) as mock_add:
        with patch("app.services.ticket_service.get_queue_size", new_callable=AsyncMock, return_value=1):
            await ticket_service.create_ticket(
                CreateTicketRequest(agency_id=agency.id, service_id=service.id),
                user_id="user-redis-test",
                org_id=org.id,
            )

    # Vérifier que service_id est bien passé à add_to_queue
    call_args = mock_add.call_args
    assert call_args is not None
    # Signature : add_to_queue(org_id, agency_id, service_id, ticket_id, priority_score)
    positional = call_args.args
    assert len(positional) >= 3
    assert positional[2] == service.id, "service_id doit être le 3ème argument de add_to_queue"


@pytest.mark.asyncio
async def test_cancel_ticket_passes_service_id_to_remove_from_queue(ticket_service, db_session, mock_redis):
    """cancel_ticket() passe ticket.service_id à remove_from_queue()."""
    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)
    user_id = "user-cancel-redis"

    with patch("app.services.ticket_service.add_to_queue", new_callable=AsyncMock, return_value=1):
        with patch("app.services.ticket_service.get_queue_size", new_callable=AsyncMock, return_value=1):
            ticket = await ticket_service.create_ticket(
                CreateTicketRequest(agency_id=agency.id, service_id=service.id),
                user_id=user_id,
                org_id=org.id,
            )

    with patch("app.services.ticket_service.remove_from_queue", new_callable=AsyncMock) as mock_remove:
        await ticket_service.cancel_ticket(ticket.id, user_id, org.id)

    call_args = mock_remove.call_args
    assert call_args is not None
    # Signature : remove_from_queue(org_id, agency_id, service_id, ticket_id)
    positional = call_args.args
    assert len(positional) >= 3
    assert positional[2] == service.id, "service_id doit être le 3ème argument de remove_from_queue"


def test_callnextrequest_requires_service_id():
    """CallNextRequest sans service_id lève une ValidationError."""
    from pydantic import ValidationError
    from app.schemas.ticket import CallNextRequest

    with pytest.raises(ValidationError):
        CallNextRequest(agency_id="a-1", counter_id="c-1", counter_name="G1")


def test_callnextrequest_valid_with_service_id():
    """CallNextRequest avec service_id est valide."""
    from app.schemas.ticket import CallNextRequest

    req = CallNextRequest(
        agency_id="a-1",
        service_id="s-1",
        counter_id="c-1",
        counter_name="Guichet 1",
    )
    assert req.service_id == "s-1"


# ── Fix : clients OTP sans organisation (User.org_id = NULL) ──────────────────
# Régression : get/cancel/return renvoyaient 404 car la recherche filtrait
# Ticket.org_id == None. Les clients sont retrouvés par propriétaire (user_id).

async def _create_ticket_for_org_less_client(ticket_service, db_session, user_id: str):
    """Crée org + branch + service, puis un ticket pour un client sans org."""
    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)
    ticket = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency.id, service_id=service.id),
        user_id=user_id,
        org_id=None,  # client OTP : pas d'organisation
    )
    return org, agency, service, ticket


@pytest.mark.asyncio
async def test_org_less_client_can_get_own_ticket(ticket_service, db_session, mock_queue):
    """Un client sans org retrouve son propre ticket (plus de 404)."""
    org, agency, service, created = await _create_ticket_for_org_less_client(
        ticket_service, db_session, "client-otp-get"
    )

    result = await ticket_service.get_ticket(created.id, "client-otp-get", None)

    assert result.id == created.id
    assert result.number == created.number
    assert result.status == "waiting"


@pytest.mark.asyncio
async def test_org_less_client_cannot_get_other_clients_ticket(ticket_service, db_session, mock_queue):
    """Un client sans org ne peut pas lire le ticket d'un autre → 404 (pas de fuite)."""
    _, _, _, created = await _create_ticket_for_org_less_client(
        ticket_service, db_session, "client-owner"
    )

    with pytest.raises(HTTPException) as exc_info:
        await ticket_service.get_ticket(created.id, "client-intruder", None)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["code"] == "TICKET_NOT_FOUND"


@pytest.mark.asyncio
async def test_org_less_client_can_cancel_and_queue_uses_ticket_org(ticket_service, db_session, mock_queue):
    """L'annulation fonctionne et la file Redis est celle de l'org du ticket."""
    org, agency, service, created = await _create_ticket_for_org_less_client(
        ticket_service, db_session, "client-otp-cancel"
    )

    with patch(
        "app.services.ticket_service.remove_from_queue", new_callable=AsyncMock
    ) as remove_mock:
        result = await ticket_service.cancel_ticket(created.id, "client-otp-cancel", None)

    assert result["success"] is True
    # remove_from_queue(org_id, agency_id, service_id, ticket_id) — org du ticket, pas None
    assert remove_mock.call_args.args[0] == org.id
    assert remove_mock.call_args.args[1] == agency.id
    assert remove_mock.call_args.args[2] == service.id


@pytest.mark.asyncio
async def test_org_less_client_cannot_cancel_other_clients_ticket(ticket_service, db_session, mock_queue):
    """Un client sans org ne peut pas annuler le ticket d'un autre → 404."""
    _, _, _, created = await _create_ticket_for_org_less_client(
        ticket_service, db_session, "client-owner-2"
    )

    with pytest.raises(HTTPException) as exc_info:
        await ticket_service.cancel_ticket(created.id, "client-intruder-2", None)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_org_less_client_can_return_to_queue_from_absent(ticket_service, db_session, mock_queue):
    """Un client absent sans org revient en file (ABSENT → WAITING)."""
    from sqlalchemy import select
    from app.models.ticket import Ticket

    org, agency, service, created = await _create_ticket_for_org_less_client(
        ticket_service, db_session, "client-otp-return"
    )
    result = await db_session.execute(select(Ticket).where(Ticket.id == created.id))
    ticket = result.scalar_one()
    ticket.status = TicketStatus.ABSENT
    await db_session.commit()

    with patch(
        "app.services.ticket_service.add_to_queue", new_callable=AsyncMock, return_value=1
    ) as add_mock:
        resp = await ticket_service.return_to_queue(
            created.id, "", user_id="client-otp-return"
        )

    assert resp["success"] is True
    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.WAITING
    assert add_mock.call_args.args[0] == org.id


@pytest.mark.asyncio
async def test_return_to_queue_rejects_other_clients_ticket(ticket_service, db_session, mock_queue):
    """Un client ne peut pas remettre en file le ticket d'un autre → 404."""
    _, _, _, created = await _create_ticket_for_org_less_client(
        ticket_service, db_session, "client-owner-3"
    )

    with pytest.raises(HTTPException) as exc_info:
        await ticket_service.return_to_queue(created.id, "", user_id="client-intruder-3")

    assert exc_info.value.status_code == 404
