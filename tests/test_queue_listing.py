"""
Tests — liste de la file d'attente côté staff (constat P1-8 de l'audit).

Aucun endpoint ne renvoyait les tickets en attente d'un service : le staff ne
pouvait ni voir qui attendait, ni repérer un ticket fantôme (il lui fallait son
UUID), ni anticiper le prochain appel. Ces tests couvrent la nouvelle route
`GET /api/v1/tickets/queue` et le service qui la porte.

Particularité assumée : `list_queue` ne touche PAS Redis. L'ordre est recalculé
depuis PostgreSQL avec la même clé que `queue_score` (priorité, puis ancienneté).
C'est ce qui rend la supervision utilisable pendant un incident Redis — et ce
qui permet à ces tests de tourner sans Redis.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.models.branch import Branch, Service
from app.models.counter import Counter
from app.models.employee import Employee
from app.models.organization import Organization
from app.models.ticket import Ticket, TicketPriority, TicketStatus
from app.models.user import User, UserRole
from app.services.ticket_service import TicketService

from tests.http_helpers import api, real_redis  # noqa: F401  (fixtures importées par nom)

NOW = datetime.now(timezone.utc)


# ── Décor commun ─────────────────────────────────────────────────────────────

async def _org(db_session, slug=None):
    slug = slug or f"org-{uuid.uuid4().hex[:8]}"
    org = Organization(name=f"Banque {slug}", slug=slug, sector="bank")
    db_session.add(org)
    await db_session.commit()
    await db_session.refresh(org)
    return org


async def _branch(db_session, org_id, name="Agence Centre"):
    branch = Branch(
        org_id=org_id, name=name, slug=f"b-{uuid.uuid4().hex[:8]}", sector="bank",
        is_active=True, is_open=True, opens_at="00:00", closes_at="23:59",
    )
    db_session.add(branch)
    await db_session.commit()
    await db_session.refresh(branch)
    return branch


async def _service(db_session, org_id, branch_id, name="Dépôt", prefix="A"):
    service = Service(
        org_id=org_id, branch_id=branch_id, name=name,
        ticket_prefix=prefix, avg_duration_minutes=5,
    )
    db_session.add(service)
    await db_session.commit()
    await db_session.refresh(service)
    return service


async def _user(db_session, org_id, role, branch_id=None, counter_id=None):
    """Utilisateur staff ; rattaché à une branch via un Employee actif."""
    user = User(
        phone=f"+257{uuid.uuid4().int % 10**8:08d}", role=role, org_id=org_id,
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    if branch_id or counter_id:
        db_session.add(Employee(org_id=org_id, user_id=user.id, agency_id=branch_id,
                                counter_id=counter_id, role=role))
        await db_session.commit()
    return user


async def _counter(db_session, org_id, branch_id, name="Guichet 1"):
    counter = Counter(org_id=org_id, agency_id=branch_id, name=name, is_open=True, is_active=True)
    db_session.add(counter)
    await db_session.commit()
    await db_session.refresh(counter)
    return counter


async def _ticket(db_session, org_id, branch_id, service_id, number, *,
                  status=TicketStatus.WAITING, priority=TicketPriority.STANDARD,
                  requested=None, created_at=None, called_at=None, served_at=None,
                  counter_name=None, user_id=None):
    ticket = Ticket(
        org_id=org_id, agency_id=branch_id, service_id=service_id,
        user_id=user_id or f"client-{number}",
        number=number, prefix=number.split("-")[0], sequence=int(number.split("-")[1]),
        status=status, priority=priority, requested_priority=requested,
        created_at=created_at or NOW, called_at=called_at, served_at=served_at,
        counter_name=counter_name,
    )
    db_session.add(ticket)
    await db_session.commit()
    await db_session.refresh(ticket)
    return ticket


# ══ Ordre et positions ═══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_la_file_est_ordonnee_comme_elle_sera_appelee(db_session):
    """
    Priorité décroissante puis FIFO — exactement la clé de `queue_score`.

    La première ligne doit être le prochain ticket appelé : c'est tout l'intérêt
    de la route pour un superviseur qui veut anticiper.
    """
    org = await _org(db_session)
    branch = await _branch(db_session, org.id)
    service = await _service(db_session, org.id, branch.id)
    agent = await _user(db_session, org.id, UserRole.AGENT, branch.id)

    standard_old = await _ticket(db_session, org.id, branch.id, service.id, "A-001",
                                 created_at=NOW - timedelta(minutes=30))
    standard_new = await _ticket(db_session, org.id, branch.id, service.id, "A-002",
                                 created_at=NOW - timedelta(minutes=20))
    vip = await _ticket(db_session, org.id, branch.id, service.id, "A-003",
                        priority=TicketPriority.VIP, created_at=NOW - timedelta(minutes=5))
    emergency = await _ticket(db_session, org.id, branch.id, service.id, "A-004",
                              priority=TicketPriority.EMERGENCY,
                              created_at=NOW - timedelta(minutes=1))

    queue = await TicketService(db_session).list_queue(branch.id, service.id, agent)

    assert [e.number for e in queue.entries] == ["A-004", "A-003", "A-001", "A-002"]
    assert [e.position for e in queue.entries] == [1, 2, 3, 4]
    assert queue.waiting_count == 4
    assert queue.entries[0].ticket_id == emergency.id
    assert standard_old.id and standard_new.id  # les deux tickets sont bien persistés


@pytest.mark.asyncio
async def test_anciennete_a_priorite_egale(db_session):
    """À priorité égale, le premier arrivé est premier servi."""
    org = await _org(db_session)
    branch = await _branch(db_session, org.id)
    service = await _service(db_session, org.id, branch.id)
    agent = await _user(db_session, org.id, UserRole.AGENT, branch.id)

    await _ticket(db_session, org.id, branch.id, service.id, "A-001",
                  created_at=NOW - timedelta(minutes=10))
    await _ticket(db_session, org.id, branch.id, service.id, "A-002",
                  created_at=NOW - timedelta(minutes=5))

    queue = await TicketService(db_session).list_queue(branch.id, service.id, agent)
    assert [e.number for e in queue.entries] == ["A-001", "A-002"]


@pytest.mark.asyncio
async def test_les_tickets_appeles_et_en_service_suivent_la_file(db_session):
    """
    WAITING d'abord, puis CALLED, puis SERVING — avec `position = 0`.

    Un superviseur doit voir les deux populations dans une seule réponse : qui
    attend encore, et qui est déjà au guichet.
    """
    org = await _org(db_session)
    branch = await _branch(db_session, org.id)
    service = await _service(db_session, org.id, branch.id)
    agent = await _user(db_session, org.id, UserRole.AGENT, branch.id)

    waiting = await _ticket(db_session, org.id, branch.id, service.id, "A-001")
    called = await _ticket(db_session, org.id, branch.id, service.id, "A-002",
                           status=TicketStatus.CALLED, called_at=NOW,
                           counter_name="Guichet 1")
    serving = await _ticket(db_session, org.id, branch.id, service.id, "A-003",
                            status=TicketStatus.SERVING, called_at=NOW,
                            served_at=NOW, counter_name="Guichet 2")

    queue = await TicketService(db_session).list_queue(branch.id, service.id, agent)

    assert [e.number for e in queue.entries] == ["A-001", "A-002", "A-003"]
    assert queue.entries[0].position == 1
    assert queue.entries[1].position == 0, "un ticket appelé n'est plus dans la file"
    assert queue.entries[2].position == 0
    assert (queue.waiting_count, queue.called_count, queue.serving_count) == (1, 1, 1)
    assert queue.entries[1].counter_name == "Guichet 1"
    assert waiting.id and called.id and serving.id


@pytest.mark.asyncio
async def test_les_tickets_termines_ne_figurent_pas_dans_la_file(db_session):
    """DONE / CANCELLED / ABSENT / TRANSFERRED sont hors file par définition."""
    org = await _org(db_session)
    branch = await _branch(db_session, org.id)
    service = await _service(db_session, org.id, branch.id)
    agent = await _user(db_session, org.id, UserRole.AGENT, branch.id)

    await _ticket(db_session, org.id, branch.id, service.id, "A-001")
    for i, status in enumerate(
        [TicketStatus.DONE, TicketStatus.CANCELLED, TicketStatus.ABSENT,
         TicketStatus.TRANSFERRED], start=2
    ):
        await _ticket(db_session, org.id, branch.id, service.id, f"A-{i:03d}", status=status)

    queue = await TicketService(db_session).list_queue(branch.id, service.id, agent)
    assert [e.number for e in queue.entries] == ["A-001"]


# ══ Contenu des lignes ═══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_anciennete_mesuree_depuis_l_entree_dans_l_etat_courant(db_session):
    """
    Un ticket rappelé après une absence ne doit pas afficher sa vieille attente.

    L'ancienneté se compte depuis `called_at`/`served_at` pour un ticket au
    guichet, depuis `created_at` seulement pour un ticket en file.

    L'instant de référence est pris ICI, pas au niveau module : la suite complète
    dure plus de dix minutes, et un `NOW` figé à l'import ferait vieillir
    artificiellement les tickets jusqu'à faire échouer l'assertion.
    """
    now = datetime.now(timezone.utc)
    org = await _org(db_session)
    branch = await _branch(db_session, org.id)
    service = await _service(db_session, org.id, branch.id)
    agent = await _user(db_session, org.id, UserRole.AGENT, branch.id)

    # Créé il y a 40 min, rappelé il y a 2 min.
    await _ticket(db_session, org.id, branch.id, service.id, "A-001",
                  status=TicketStatus.CALLED,
                  created_at=now - timedelta(minutes=40),
                  called_at=now - timedelta(minutes=2))

    queue = await TicketService(db_session).list_queue(branch.id, service.id, agent)
    assert 1 <= queue.entries[0].waiting_since_minutes <= 3


@pytest.mark.asyncio
async def test_moyenne_d_attente_et_demande_de_priorite(db_session):
    """`average_wait_minutes` est la moyenne observée des tickets en file."""
    now = datetime.now(timezone.utc)
    org = await _org(db_session)
    branch = await _branch(db_session, org.id)
    service = await _service(db_session, org.id, branch.id)
    agent = await _user(db_session, org.id, UserRole.AGENT, branch.id)

    await _ticket(db_session, org.id, branch.id, service.id, "A-001",
                  created_at=now - timedelta(minutes=20),
                  requested=TicketPriority.VIP)
    await _ticket(db_session, org.id, branch.id, service.id, "A-002",
                  created_at=now - timedelta(minutes=10))

    queue = await TicketService(db_session).list_queue(branch.id, service.id, agent)
    assert 14 <= queue.average_wait_minutes <= 16
    assert queue.entries[0].requested_priority == "vip"
    assert queue.entries[0].priority == "standard", "la demande n'est pas encore appliquée"
    assert queue.entries[1].requested_priority is None


@pytest.mark.asyncio
async def test_file_vide_renvoie_une_reponse_structuree(db_session):
    """Pas de 500 ni de `null` : une file vide est une information, pas une erreur."""
    org = await _org(db_session)
    branch = await _branch(db_session, org.id)
    service = await _service(db_session, org.id, branch.id)
    agent = await _user(db_session, org.id, UserRole.AGENT, branch.id)

    queue = await TicketService(db_session).list_queue(branch.id, service.id, agent)

    assert queue.entries == []
    assert queue.waiting_count == 0
    assert queue.average_wait_minutes is None
    assert queue.service_name == "Dépôt"


# ══ Périmètre et isolation ═══════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_un_agent_ne_voit_pas_la_file_d_une_autre_agence(db_session):
    """Périmètre branch : un agent de l'Annexe ne supervise pas le Centre-ville."""
    org = await _org(db_session)
    centre = await _branch(db_session, org.id, "Centre-ville")
    annexe = await _branch(db_session, org.id, "Annexe")
    service = await _service(db_session, org.id, centre.id)
    agent_annexe = await _user(db_session, org.id, UserRole.AGENT, annexe.id)
    await _ticket(db_session, org.id, centre.id, service.id, "A-001")

    with pytest.raises(HTTPException) as exc:
        await TicketService(db_session).list_queue(centre.id, service.id, agent_annexe)

    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "BRANCH_FORBIDDEN"


@pytest.mark.asyncio
async def test_un_admin_org_voit_toutes_ses_agences(db_session):
    """ADMIN_ORG n'est pas limité à une branche : il supervise son organisation."""
    org = await _org(db_session)
    centre = await _branch(db_session, org.id, "Centre-ville")
    service = await _service(db_session, org.id, centre.id)
    admin = await _user(db_session, org.id, UserRole.ADMIN_ORG)
    await _ticket(db_session, org.id, centre.id, service.id, "A-001")

    queue = await TicketService(db_session).list_queue(centre.id, service.id, admin)
    assert queue.waiting_count == 1


@pytest.mark.asyncio
async def test_un_admin_org_ne_voit_pas_les_agences_d_une_autre_org(db_session):
    """Isolation multi-tenant : l'agence d'une autre organisation reste invisible (404)."""
    org_a = await _org(db_session)
    org_b = await _org(db_session)
    branch_b = await _branch(db_session, org_b.id)
    service_b = await _service(db_session, org_b.id, branch_b.id)
    admin_a = await _user(db_session, org_a.id, UserRole.ADMIN_ORG)

    with pytest.raises(HTTPException) as exc:
        await TicketService(db_session).list_queue(branch_b.id, service_b.id, admin_a)

    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "BRANCH_NOT_FOUND"


@pytest.mark.asyncio
async def test_un_client_ne_peut_pas_lister_la_file(db_session):
    """Garde dans le service : un CLIENT n'a rien à faire ici (403 STAFF_ONLY)."""
    org = await _org(db_session)
    branch = await _branch(db_session, org.id)
    service = await _service(db_session, org.id, branch.id)
    client = await _user(db_session, None, UserRole.CLIENT)

    with pytest.raises(HTTPException) as exc:
        await TicketService(db_session).list_queue(branch.id, service.id, client)

    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "STAFF_ONLY"


@pytest.mark.asyncio
async def test_un_service_d_une_autre_agence_est_introuvable(db_session):
    """Un service appartient à UNE branch : le demander via une autre renvoie 404."""
    org = await _org(db_session)
    centre = await _branch(db_session, org.id, "Centre-ville")
    annexe = await _branch(db_session, org.id, "Annexe")
    service_centre = await _service(db_session, org.id, centre.id)
    admin = await _user(db_session, org.id, UserRole.ADMIN_ORG)

    with pytest.raises(HTTPException) as exc:
        await TicketService(db_session).list_queue(annexe.id, service_centre.id, admin)

    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "SERVICE_NOT_FOUND"


# ══ Contrat HTTP ═════════════════════════════════════════════════════════════

def test_route_exposee_et_protegee_par_role_agent():
    """
    La route existe, est déclarée AVANT `/{ticket_id}` et exige le rôle AGENT.

    L'ordre de déclaration compte : placée après `/{ticket_id}`, FastAPI
    interpréterait « queue » comme un identifiant de ticket et renverrait 404.
    """
    from app.core.deps import get_current_agent
    from app.routers.tickets import router

    paths = [r.path for r in router.routes if hasattr(r, "path")]
    assert "/queue" in paths
    assert paths.index("/queue") < paths.index("/{ticket_id}"), (
        "/queue doit être déclarée avant /{ticket_id}, sinon elle est capturée "
        "par le paramètre de chemin"
    )

    route = next(r for r in router.routes if getattr(r, "path", None) == "/queue")
    dependency_calls = {d.call for d in route.dependant.dependencies}
    assert get_current_agent in dependency_calls


# ══ Bout en bout (pile complète : routers + JWT + RBAC + vrai Redis) ═════════

@pytest.mark.asyncio
async def test_http_un_agent_lit_la_file_de_son_service(api, db_session):
    """Le poste guichet appelle la route réelle et reçoit la file ordonnée."""
    from tests.http_helpers import auth, make_world

    org, branch, service, counter, agent, client = await make_world(db_session)
    created = await api.post("/api/v1/tickets",
                             json={"agency_id": branch.id, "service_id": service.id},
                             headers=auth(client))
    assert created.status_code == 200, created.text

    response = await api.get(
        "/api/v1/tickets/queue",
        params={"agency_id": branch.id, "service_id": service.id},
        headers=auth(agent),
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["waiting_count"] == 1
    assert data["entries"][0]["number"] == created.json()["number"]
    assert data["entries"][0]["position"] == 1
    assert data["service_name"] == "Dépôt"


@pytest.mark.asyncio
async def test_http_un_client_est_refuse_sur_la_file(api, db_session):
    """RBAC réel : le client reçoit 403, l'agent passe."""
    from tests.http_helpers import auth, make_world

    org, branch, service, counter, agent, client = await make_world(db_session)

    response = await api.get(
        "/api/v1/tickets/queue",
        params={"agency_id": branch.id, "service_id": service.id},
        headers=auth(client),
    )

    assert response.status_code == 403
