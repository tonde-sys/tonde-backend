"""
P2-14 — `get_branch_display` : suppression du N+1.

Constat : une requête par guichet et trois par service, soit ~30 requêtes pour
12 guichets et 6 services. Cet endpoint est PUBLIC et interrogé toutes les 7 s
par chaque poste de l'agence : pour 20 postes, cela faisait ~85 requêtes/s sur
une seule agence, pour un écran qui n'a pas besoin d'être frais à la seconde.

Le test central n'est pas « la réponse est correcte » (les autres le font) mais
« le nombre de requêtes ne dépend pas du nombre de guichets ni de services ».
C'est la seule formulation qui ne se dégrade pas au prochain service ajouté.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import event

from app.models.branch import Branch, Service
from app.models.counter import Counter
from app.models.organization import Organization
from app.models.ticket import Ticket, TicketPriority, TicketStatus
from app.models.user import User
from app.services import public_service as public_service_module
from app.services.public_service import PublicQueueService
from app.services.ticket_service import ETA_SAMPLE_SIZE, TicketService


# ── Outils ────────────────────────────────────────────────────────────────────

class QueryCounter:
    """Compte les instructions SQL réellement émises (pas les appels ORM)."""

    def __init__(self, engine):
        self.engine = engine
        self.statements: list[str] = []

    def __enter__(self):
        event.listen(self.engine.sync_engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc_info):
        event.remove(self.engine.sync_engine, "before_cursor_execute", self._record)
        return False

    def _record(self, conn, cursor, statement, parameters, context, executemany):
        self.statements.append(statement)


async def _make_org(db) -> Organization:
    org = Organization(name="Banque Test", slug=f"org-{uuid.uuid4().hex[:8]}", sector="bank")
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return org


async def _make_branch(db, org_id: str) -> Branch:
    branch = Branch(
        org_id=org_id, name="Agence Centre", slug=f"b-{uuid.uuid4().hex[:8]}",
        sector="bank", is_active=True, is_open=True,
        opens_at="00:00", closes_at="23:59",
    )
    db.add(branch)
    await db.commit()
    await db.refresh(branch)
    return branch


async def _make_service(db, org_id: str, branch_id: str, prefix: str, name: str,
                        avg: int = 5) -> Service:
    service = Service(org_id=org_id, branch_id=branch_id, name=name,
                      ticket_prefix=prefix, avg_duration_minutes=avg)
    db.add(service)
    await db.commit()
    await db.refresh(service)
    return service


async def _make_counter(db, org_id: str, branch_id: str, name: str) -> Counter:
    counter = Counter(org_id=org_id, agency_id=branch_id, name=name, is_open=True)
    db.add(counter)
    await db.commit()
    await db.refresh(counter)
    return counter


async def _make_user(db) -> User:
    user = User(phone=f"+257{uuid.uuid4().int % 10**8:08d}", role="client", is_verified=True)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_ticket(db, org_id: str, branch_id: str, service: Service, user_id: str,
                       sequence: int, status: TicketStatus = TicketStatus.WAITING,
                       *, called_at=None, done_at=None, service_seconds=None) -> Ticket:
    ticket = Ticket(
        number=f"{service.ticket_prefix}-{sequence:03d}",
        prefix=service.ticket_prefix,
        sequence=sequence,
        user_id=user_id,
        org_id=org_id,
        agency_id=branch_id,
        service_id=service.id,
        status=status,
        priority=TicketPriority.STANDARD,
        called_at=called_at,
        done_at=done_at,
        service_seconds=service_seconds,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    return ticket


@pytest_asyncio.fixture
async def agence(db_session):
    """
    Une agence type : 2 services, 2 guichets, des tickets dans tous les états.

    Dépôt (A) : 2 en attente, 1 appelé, 3 terminés.
    Crédit (B) : rien.
    Guichet 1 traite A-003, guichet 2 est libre.
    """
    org = await _make_org(db_session)
    branch = await _make_branch(db_session, org.id)
    depot = await _make_service(db_session, org.id, branch.id, "A", "Dépôt", avg=5)
    credit = await _make_service(db_session, org.id, branch.id, "B", "Crédit", avg=12)
    user = await _make_user(db_session)

    now = datetime.now(timezone.utc)
    await _make_ticket(db_session, org.id, branch.id, depot, user.id, 1)
    await _make_ticket(db_session, org.id, branch.id, depot, user.id, 2)
    appele = await _make_ticket(
        db_session, org.id, branch.id, depot, user.id, 3,
        TicketStatus.CALLED, called_at=now - timedelta(minutes=2),
    )
    for i in range(3):
        await _make_ticket(
            db_session, org.id, branch.id, depot, user.id, 10 + i,
            TicketStatus.DONE,
            called_at=now - timedelta(hours=2),
            done_at=now - timedelta(hours=1, minutes=i),
            service_seconds=180 + 60 * i,
        )

    guichet1 = await _make_counter(db_session, org.id, branch.id, "Guichet 1")
    guichet1.current_ticket_id = appele.id
    await db_session.commit()
    await db_session.refresh(guichet1)
    guichet2 = await _make_counter(db_session, org.id, branch.id, "Guichet 2")

    return org, branch, depot, credit, guichet1, guichet2, appele


# ══ La réponse reste juste ════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_ecran_de_salle_renvoie_les_bonnes_donnees(agence, db_session, mock_redis):
    """Numéro en cours, dernier appelé et ETA restent corrects après regroupement."""
    org, branch, depot, credit, guichet1, guichet2, appele = agence

    data = await PublicQueueService(db_session).get_branch_display(branch.id)

    assert data["branch"]["id"] == branch.id
    assert data["branch"]["is_open"] is True

    # Guichets triés par nom : Guichet 1 traite A-003, Guichet 2 ne traite rien.
    par_nom = {row["name"]: row for row in data["counters"]}
    assert par_nom["Guichet 1"]["now_serving"] == "A-003"
    assert par_nom["Guichet 2"]["now_serving"] is None

    # Services triés par préfixe.
    assert [row["ticket_prefix"] for row in data["services"]] == ["A", "B"]
    depot_row = data["services"][0]
    assert depot_row["last_called_number"] == "A-003", "dernier ticket appelé du service"
    assert depot_row["waiting"] == 1, "valeur Redis (zcard mocké à 1)"

    # Aucune donnée personnelle ne doit sortir par cet endpoint public.
    for row in data["services"] + data["counters"]:
        assert "user_id" not in row and "phone" not in row


@pytest.mark.asyncio
async def test_dernier_appele_ignore_les_tickets_jamais_appeles(agence, db_session, mock_redis):
    """Un ticket en attente n'a pas de `called_at` : il ne doit pas devenir le « dernier appelé »."""
    org, branch, depot, credit, guichet1, guichet2, appele = agence

    data = await PublicQueueService(db_session).get_branch_display(branch.id)
    par_prefixe = {row["ticket_prefix"]: row for row in data["services"]}

    assert par_prefixe["A"]["last_called_number"] == "A-003"
    assert par_prefixe["B"]["last_called_number"] is None, "service sans appel"


@pytest.mark.asyncio
async def test_attente_estimee_coherente_avec_le_calcul_unitaire(agence, db_session, mock_redis):
    """
    `_eta_minutes` (lot) doit donner le même résultat que `estimate_wait_minutes`
    (unitaire) : le regroupement ne doit pas changer la formule, seulement le
    nombre de requêtes.
    """
    org, branch, depot, credit, guichet1, guichet2, appele = agence
    service = TicketService(db_session)

    data = await PublicQueueService(db_session).get_branch_display(branch.id)
    par_prefixe = {row["ticket_prefix"]: row for row in data["services"]}

    for prefixe, row in par_prefixe.items():
        attendu = await service.estimate_wait_minutes(
            depot if prefixe == "A" else credit, row["waiting"] + 1
        )
        assert row["estimated_wait_minutes"] == attendu, prefixe


# ══ Le point du constat : le coût ne dépend plus de la taille de l'agence ════

@pytest.mark.asyncio
async def test_nombre_de_requetes_constant_quel_que_soit_le_nombre_de_services(
    agence, db_session, db_engine, mock_redis
):
    """
    Deux services ou huit : exactement le même nombre de requêtes.

    C'est la formulation qui protège l'avenir. Une assertion du type « moins de
    10 requêtes » passerait aujourd'hui et sauterait à la première agence un peu
    grande ; celle-ci échoue dès qu'un N+1 revient.
    """
    org, branch, depot, credit, guichet1, guichet2, appele = agence
    ecran = PublicQueueService(db_session)

    with QueryCounter(db_engine) as petit:
        await ecran.get_branch_display(branch.id)

    # On ajoute 6 services et 4 guichets à la même agence.
    for index in range(6):
        service = await _make_service(
            db_session, org.id, branch.id, chr(ord("D") + index), f"Service {index}"
        )
        await _make_ticket(db_session, org.id, branch.id, service, appele.user_id, 1)
    for index in range(4):
        await _make_counter(db_session, org.id, branch.id, f"Guichet {index + 3}")

    with QueryCounter(db_engine) as grand:
        data = await ecran.get_branch_display(branch.id)

    assert len(data["services"]) == 8
    assert len(data["counters"]) == 6
    assert len(grand.statements) == len(petit.statements), (
        f"{len(petit.statements)} requêtes pour 2 services, "
        f"{len(grand.statements)} pour 8 — il reste un N+1 :\n"
        + "\n".join(grand.statements)
    )
    assert len(grand.statements) <= 6, "\n".join(grand.statements)


@pytest.mark.asyncio
async def test_repli_redis_ne_cree_pas_de_n_plus_un(agence, db_session, db_engine, monkeypatch):
    """
    Panne Redis : le repli SQL doit rester une requête groupée.

    La version précédente faisait `count(*)` par service dans le `except` : une
    panne Redis se transformait en N+1 sur PostgreSQL, c'est-à-dire une panne
    Redis transformée en surcharge de la base — au pire moment.
    """
    org, branch, depot, credit, guichet1, guichet2, appele = agence

    async def redis_ko(*_args, **_kwargs):
        raise ConnectionError("Redis injoignable")

    monkeypatch.setattr(public_service_module, "get_queue_size", redis_ko)
    ecran = PublicQueueService(db_session)

    with QueryCounter(db_engine) as petit:
        data = await ecran.get_branch_display(branch.id)
    assert {row["ticket_prefix"]: row["waiting"] for row in data["services"]}["A"] == 2

    for index in range(6):
        service = await _make_service(
            db_session, org.id, branch.id, chr(ord("D") + index), f"Service {index}"
        )
        await _make_ticket(db_session, org.id, branch.id, service, appele.user_id, 1)

    with QueryCounter(db_engine) as grand:
        data = await ecran.get_branch_display(branch.id)

    assert len(data["services"]) == 8
    assert len(grand.statements) == len(petit.statements), (
        "le repli Redis fait une requête par service :\n" + "\n".join(grand.statements)
    )


@pytest.mark.asyncio
async def test_agence_inconnue_renvoie_404(db_session):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await PublicQueueService(db_session).get_branch_display(str(uuid.uuid4()))
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["code"] == "BRANCH_NOT_FOUND"


# ══ Le regroupement ne change pas les valeurs ════════════════════════════════

@pytest.mark.asyncio
async def test_duree_moyenne_groupee_egale_au_calcul_unitaire(agence, db_session):
    """
    `avg_service_seconds_batch` (row_number) doit rendre exactement ce que rend
    `_avg_service_seconds` service par service — y compris la troncature aux
    ETA_SAMPLE_SIZE derniers tickets.
    """
    org, branch, depot, credit, guichet1, guichet2, appele = agence

    # Plus de ETA_SAMPLE_SIZE tickets terminés, pour exercer la fenêtre glissante :
    # seuls les plus récents doivent compter.
    now = datetime.now(timezone.utc)
    for i in range(ETA_SAMPLE_SIZE + 4):
        await _make_ticket(
            db_session, org.id, branch.id, credit, appele.user_id, 100 + i,
            TicketStatus.DONE,
            called_at=now - timedelta(hours=5),
            done_at=now - timedelta(minutes=ETA_SAMPLE_SIZE + 4 - i),
            service_seconds=60 * (i + 1),
        )

    service = TicketService(db_session)
    groupe = await service.avg_service_seconds_batch([depot, credit])

    for cible in (depot, credit):
        unitaire = await service._avg_service_seconds(cible.id, cible.avg_duration_minutes)
        assert groupe[cible.id] == unitaire, cible.name


@pytest.mark.asyncio
async def test_ticket_termine_sans_date_de_fin_est_ignore(agence, db_session):
    """
    Un ticket DONE sans `done_at` ne peut pas être ordonné. PostgreSQL place les
    NULL EN TÊTE d'un tri descendant : sans filtre explicite, ce ticket
    compterait comme « le plus récent » et fausserait la moyenne. Les deux
    chemins (unitaire et groupé) doivent l'ignorer de la même façon.
    """
    org, branch, depot, credit, guichet1, guichet2, appele = agence
    await _make_ticket(
        db_session, org.id, branch.id, credit, appele.user_id, 200,
        TicketStatus.DONE, done_at=None, service_seconds=3600,
    )

    service = TicketService(db_session)
    unitaire = await service._avg_service_seconds(credit.id, credit.avg_duration_minutes)
    groupe = await service.avg_service_seconds_batch([credit])

    # Aucun échantillon retenu : la moyenne est celle de la durée configurée (12 min).
    assert unitaire == 12 * 60
    assert groupe[credit.id] == 12 * 60
