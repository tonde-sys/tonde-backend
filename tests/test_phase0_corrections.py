"""
Tests — lot « phase 0 » de l'audit du 15/11 + décision d'expiration.

Couvre quatre corrections décidées avec l'exploitant :

  P1-7   Fermer un guichet ne doit pas abandonner le client appelé.
  P0-5   Désactiver un employé ne doit pas retirer un rôle obtenu ailleurs.
  P1-12  L'historique doit donner la vraie position d'un ticket encore en file.
  P0-4   L'expiration se fait à la FERMETURE de l'agence, plus sur une durée fixe.

Ces tests n'ont besoin ni de PostgreSQL ni de Redis : la file est simulée
(fixture `mock_queue`). C'est volontaire — les comportements testés ici sont de
la logique métier, pas de l'atomicité.
"""
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import patch, AsyncMock

from app.core.config import settings
from app.models.branch import Branch, Service
from app.models.counter import Counter
from app.models.employee import Employee, EmployeeStatus
from app.models.organization import Organization
from app.models.ticket import Ticket, TicketPriority, TicketStatus
from app.models.user import User, UserRole
from app.services.counter_service import CounterService
from app.services.employee_service import EmployeeService
from app.services.ticket_service import BURUNDI_TZ

NOW = datetime.now(timezone.utc)


@pytest.fixture
def mock_queue():
    """Neutralise les appels Redis de la file (ajout, retrait, position, taille)."""
    with patch("app.services.ticket_service.add_to_queue", new_callable=AsyncMock, return_value=1):
        with patch("app.services.ticket_service.get_queue_size", new_callable=AsyncMock, return_value=1):
            with patch("app.services.ticket_service.get_queue_position", new_callable=AsyncMock, return_value=1):
                with patch("app.services.ticket_service.remove_from_queue", new_callable=AsyncMock):
                    with patch("app.services.ticket_service.pop_next_ticket", new_callable=AsyncMock):
                        with patch("app.services.ticket_service.publish_event", new_callable=AsyncMock):
                            yield


# ── Décor commun ─────────────────────────────────────────────────────────────

async def _org(db_session, slug="org-test"):
    org = Organization(name=f"Banque {slug}", slug=slug, sector="bank")
    db_session.add(org)
    await db_session.commit()
    await db_session.refresh(org)
    return org


async def _branch(db_session, org_id, opens="00:00", closes="23:59", is_open=True):
    branch = Branch(
        org_id=org_id,
        name="Agence Centre",
        slug=f"b-{opens.replace(':', '')}-{closes.replace(':', '')}",
        sector="bank",
        is_active=True,
        is_open=is_open,
        opens_at=opens,
        closes_at=closes,
    )
    db_session.add(branch)
    await db_session.commit()
    await db_session.refresh(branch)
    return branch


async def _service(db_session, org_id, branch_id, prefix="A"):
    service = Service(
        org_id=org_id, branch_id=branch_id, name="Dépôt",
        ticket_prefix=prefix, avg_duration_minutes=5,
    )
    db_session.add(service)
    await db_session.commit()
    await db_session.refresh(service)
    return service


async def _counter(db_session, org_id, branch_id, name="Guichet 1"):
    counter = Counter(org_id=org_id, agency_id=branch_id, name=name, is_open=True, is_active=True)
    db_session.add(counter)
    await db_session.commit()
    await db_session.refresh(counter)
    return counter


async def _ticket(db_session, org_id, branch_id, service_id, number="A-001",
                  status=TicketStatus.WAITING, created_at=None):
    ticket = Ticket(
        org_id=org_id,
        agency_id=branch_id,
        service_id=service_id,
        user_id=f"client-{number}",
        number=number,
        prefix=number.split("-")[0],
        sequence=int(number.split("-")[1]),
        status=status,
        priority=TicketPriority.STANDARD,
        created_at=created_at or NOW,
    )
    db_session.add(ticket)
    await db_session.commit()
    await db_session.refresh(ticket)
    return ticket


# ══ P1-7 — Fermeture d'un guichet ════════════════════════════════════════════

@pytest.mark.asyncio
async def test_fermer_un_guichet_remet_le_client_appele_en_file(db_session, mock_queue):
    """
    Le client était présent, c'est le guichet qui ferme : il retourne en file.

    Avant ce correctif, il restait CALLED, personne ne le servait, et le
    scheduler le passait en ABSENT 180 s plus tard — 30 minutes de pénalité
    pour un client qui n'avait rien fait.
    """
    org = await _org(db_session)
    branch = await _branch(db_session, org.id)
    service = await _service(db_session, org.id, branch.id)
    counter = await _counter(db_session, org.id, branch.id)
    ticket = await _ticket(db_session, org.id, branch.id, service.id, status=TicketStatus.CALLED)

    ticket.counter_id = counter.id
    ticket.counter_name = counter.name
    ticket.called_at = NOW
    counter.current_ticket_id = ticket.id
    await db_session.commit()

    await CounterService(db_session).close(counter.id, org.id)

    await db_session.refresh(ticket)
    await db_session.refresh(counter)
    assert ticket.status == TicketStatus.WAITING
    assert ticket.counter_id is None, "le ticket ne doit plus désigner le guichet fermé"
    assert ticket.counter_name is None
    assert ticket.called_at is None, "l'échéance d'absence ne doit plus courir"
    assert counter.current_ticket_id is None


@pytest.mark.asyncio
async def test_fermer_un_guichet_sans_client_ne_touche_a_rien(db_session, mock_queue):
    """Cas courant : le guichet n'a personne. La fermeture reste possible."""
    org = await _org(db_session, "org-vide")
    branch = await _branch(db_session, org.id)
    counter = await _counter(db_session, org.id, branch.id)

    result = await CounterService(db_session).close(counter.id, org.id)

    assert result.is_open is False


@pytest.mark.asyncio
async def test_supprimer_un_guichet_remet_aussi_le_client_en_file(db_session, mock_queue):
    """La suppression (soft delete) ferme le guichet : même conséquence pour le client."""
    org = await _org(db_session, "org-delete")
    branch = await _branch(db_session, org.id)
    service = await _service(db_session, org.id, branch.id)
    counter = await _counter(db_session, org.id, branch.id)
    ticket = await _ticket(db_session, org.id, branch.id, service.id, status=TicketStatus.SERVING)

    ticket.counter_id = counter.id
    counter.current_ticket_id = ticket.id
    await db_session.commit()

    await CounterService(db_session).delete(counter.id, org.id)

    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.WAITING


# ══ P0-5 — Désactivation d'un employé multi-organisations ════════════════════

@pytest.mark.asyncio
async def test_desactivation_conserve_le_role_si_un_autre_rattachement_existe(db_session):
    """
    Un consultant admin d'une organisation et agent d'une autre : retirer le
    second rattachement ne doit pas lui retirer son rôle d'admin.
    """
    org_a = await _org(db_session, "org-a")
    org_b = await _org(db_session, "org-b")

    user = User(email="consultant@test.bi", name="Consultant", role=UserRole.ADMIN_ORG,
                org_id=org_a.id, is_active=True, is_verified=True)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    employee_a = Employee(org_id=org_a.id, user_id=user.id, role=UserRole.ADMIN_ORG,
                          status=EmployeeStatus.ACTIVE)
    employee_b = Employee(org_id=org_b.id, user_id=user.id, role=UserRole.AGENT,
                          status=EmployeeStatus.ACTIVE)
    db_session.add_all([employee_a, employee_b])
    await db_session.commit()

    await EmployeeService(db_session).deactivate(employee_b.id, org_b.id)

    await db_session.refresh(user)
    assert user.role == UserRole.ADMIN_ORG, "le rôle obtenu dans l'org A est conservé"
    assert user.org_id == org_a.id


@pytest.mark.asyncio
async def test_desactivation_retrograde_si_dernier_rattachement(db_session):
    """Dernier rattachement retiré : l'utilisateur redevient un simple client."""
    org = await _org(db_session, "org-seule")

    user = User(email="agent@test.bi", name="Agent", role=UserRole.AGENT,
                org_id=org.id, is_active=True, is_verified=True)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    employee = Employee(org_id=org.id, user_id=user.id, role=UserRole.AGENT,
                        status=EmployeeStatus.ACTIVE)
    db_session.add(employee)
    await db_session.commit()

    await EmployeeService(db_session).deactivate(employee.id, org.id)

    await db_session.refresh(user)
    assert user.role == UserRole.CLIENT
    assert user.org_id is None


# ══ P1-12 — Position dans l'historique ═══════════════════════════════════════

@pytest.mark.asyncio
async def test_historique_donne_la_vraie_position_d_un_ticket_en_file(
    ticket_service, db_session, mock_queue
):
    """
    Un ticket WAITING de l'historique doit afficher sa position réelle.

    Avant, `get_history` renvoyait 0 pour tout le monde : le mobile annonçait
    « position 0 » à un client qui attendait derrière six personnes.
    """
    org = await _org(db_session, "org-hist")
    branch = await _branch(db_session, org.id)
    service = await _service(db_session, org.id, branch.id)
    ticket = await _ticket(db_session, org.id, branch.id, service.id)

    with patch("app.services.ticket_service.get_queue_position",
               new_callable=AsyncMock, return_value=3):
        with patch("app.services.ticket_service.get_queue_size",
                   new_callable=AsyncMock, return_value=7):
            history = await ticket_service.get_history(ticket.user_id)

    item = history.items[0]
    assert item.position == 3
    assert item.total_in_queue == 7


@pytest.mark.asyncio
async def test_historique_ne_consulte_pas_la_file_pour_un_ticket_termine(
    ticket_service, db_session, mock_queue
):
    """Un ticket terminé n'est plus dans la file : inutile d'interroger Redis."""
    org = await _org(db_session, "org-hist-done")
    branch = await _branch(db_session, org.id)
    service = await _service(db_session, org.id, branch.id)
    ticket = await _ticket(db_session, org.id, branch.id, service.id, status=TicketStatus.DONE)

    with patch("app.services.ticket_service.get_queue_position", new_callable=AsyncMock) as position:
        with patch("app.services.ticket_service.get_queue_size", new_callable=AsyncMock) as size:
            history = await ticket_service.get_history(ticket.user_id)

    assert history.items[0].position == 0
    position.assert_not_awaited()
    size.assert_not_awaited()


# ══ P0-4 — Expiration à la fermeture de l'agence ════════════════════════════

def _local(hour: int, minute: int = 0) -> datetime:
    """Heure locale Burundi (UTC+2) du jour courant, convertie en UTC."""
    today = datetime.now(BURUNDI_TZ).date()
    return datetime(today.year, today.month, today.day, hour, minute,
                    tzinfo=BURUNDI_TZ).astimezone(timezone.utc)


@pytest.mark.asyncio
async def test_ticket_en_file_annule_a_la_fermeture(ticket_service, db_session, mock_queue):
    """17:05, l'agence a fermé : les tickets restés en file sont clos et notifiés."""
    org = await _org(db_session, "org-ferme")
    branch = await _branch(db_session, org.id, opens="08:00", closes="17:00")
    service = await _service(db_session, org.id, branch.id)
    ticket = await _ticket(db_session, org.id, branch.id, service.id,
                           created_at=_local(9, 0))

    counts = await ticket_service.expire_stale_tickets(now=_local(17, 5))

    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.CANCELLED
    assert counts["waiting"] == 1


@pytest.mark.asyncio
async def test_ticket_en_file_survit_a_une_longue_journee(ticket_service, db_session, mock_queue):
    """
    Un ticket pris à 06:05 et encore en file à 14:00 doit rester vivant.

    C'est le cœur de la décision : l'ancienne règle « 12 h » l'aurait annulé
    plus tard sans prévenir, et sur une agence ouverte plus de douze heures
    elle annulait des tickets en pleine journée.
    """
    org = await _org(db_session, "org-longue")
    branch = await _branch(db_session, org.id, opens="06:00", closes="20:00")
    service = await _service(db_session, org.id, branch.id)
    ticket = await _ticket(db_session, org.id, branch.id, service.id,
                           created_at=_local(6, 5))

    counts = await ticket_service.expire_stale_tickets(now=_local(14, 0))

    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.WAITING
    assert counts["waiting"] == 0


@pytest.mark.asyncio
async def test_agence_ouverte_24h_ne_perd_jamais_ses_tickets(ticket_service, db_session, mock_queue):
    """`opens == closes` = ouvert en permanence : rien n'est clos à la fermeture."""
    org = await _org(db_session, "org-24h")
    branch = await _branch(db_session, org.id, opens="00:00", closes="00:00")
    service = await _service(db_session, org.id, branch.id)
    ticket = await _ticket(db_session, org.id, branch.id, service.id,
                           created_at=_local(8, 0))

    counts = await ticket_service.expire_stale_tickets(now=_local(22, 0))

    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.WAITING
    assert counts["waiting"] == 0


@pytest.mark.asyncio
async def test_filet_de_securite_annule_un_ticket_oublie(ticket_service, db_session, mock_queue):
    """
    Agence ouverte en permanence mais ticket oublié depuis plus de 72 h :
    le filet de sécurité s'applique, sinon le client resterait bloqué à vie
    par la règle « un seul ticket actif ».
    """
    org = await _org(db_session, "org-oublie")
    branch = await _branch(db_session, org.id, opens="00:00", closes="00:00")
    service = await _service(db_session, org.id, branch.id)
    ticket = await _ticket(
        db_session, org.id, branch.id, service.id,
        created_at=NOW - timedelta(hours=settings.WAITING_TICKET_TTL_HOURS + 1),
    )

    counts = await ticket_service.expire_stale_tickets()

    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.CANCELLED
    assert counts["waiting"] == 1
