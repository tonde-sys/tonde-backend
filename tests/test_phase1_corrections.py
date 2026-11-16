"""
Tests — lot « phase 1 » de l'audit du 15/11.

Couvre quatre points :

  P1-11  `return_to_queue` ne doit pas laisser un ticket hors file si Redis échoue.
  P2-15  Une erreur de `get_queue_size` ne doit pas annuler un ticket déjà en file.
  P2-18  `/health/ready` doit voir un scheduler mort ou bloqué.
  (+)    `get_history` : le total de pagination ne doit pas être écrasé par la
         taille de la file (régression introduite par le correctif P1-12).

Aucun de ces tests n'a besoin d'un vrai Redis : la file est simulée. Les
comportements visés sont des décisions métier (« que répond-on quand le cache
tombe ? »), pas de l'atomicité — celle-ci est couverte ailleurs, sur vrai Redis.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import time
import uuid

import pytest
from unittest.mock import AsyncMock, patch

from app.models.branch import Branch, Service
from app.models.organization import Organization
from app.models.ticket import Ticket, TicketPriority, TicketStatus
from app.schemas.ticket import CreateTicketRequest
from app.services import health_service
from app.services.queue_scheduler import QueueScheduler
from app.services.ticket_service import TicketService

NOW = datetime.now(timezone.utc)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_queue(mock_redis):
    """Neutralise les helpers Redis de la file (le client Redis est déjà mocké)."""
    with patch("app.services.ticket_service.add_to_queue", new_callable=AsyncMock, return_value=1):
        with patch("app.services.ticket_service.get_queue_size", new_callable=AsyncMock, return_value=1):
            with patch("app.services.ticket_service.get_queue_position", new_callable=AsyncMock, return_value=1):
                with patch("app.services.ticket_service.remove_from_queue", new_callable=AsyncMock):
                    with patch("app.services.ticket_service.pop_next_ticket", new_callable=AsyncMock):
                        with patch("app.services.ticket_service.publish_event", new_callable=AsyncMock):
                            yield


class _FakeScheduler:
    """Scheduler minimal : reproduit la surface lue par la sonde de santé."""

    def __init__(self, *, running=True, last_run_at=None, started_at=None, last_error=None):
        self._running = running
        self.last_run_at = last_run_at
        self.started_at = started_at
        self.last_error = last_error

    @property
    def is_running(self) -> bool:
        return self._running


# ── Décor commun ─────────────────────────────────────────────────────────────

async def _org(db_session, slug="org-p1"):
    org = Organization(name=f"Banque {slug}", slug=slug, sector="bank")
    db_session.add(org)
    await db_session.commit()
    await db_session.refresh(org)
    return org


async def _branch(db_session, org_id, name="Agence Centre", is_open=True):
    branch = Branch(
        org_id=org_id, name=name, slug=f"b-{uuid.uuid4().hex[:8]}", sector="bank",
        is_active=True, is_open=is_open, opens_at="00:00", closes_at="23:59",
    )
    db_session.add(branch)
    await db_session.commit()
    await db_session.refresh(branch)
    return branch


async def _service(db_session, org_id, branch_id):
    service = Service(
        org_id=org_id, branch_id=branch_id, name="Dépôt",
        ticket_prefix="A", avg_duration_minutes=5,
    )
    db_session.add(service)
    await db_session.commit()
    await db_session.refresh(service)
    return service


async def _ticket(db_session, org_id, branch_id, service_id, number="A-001",
                  status=TicketStatus.WAITING, user_id="client-p1", created_at=None):
    ticket = Ticket(
        org_id=org_id, agency_id=branch_id, service_id=service_id, user_id=user_id,
        number=number, prefix=number.split("-")[0], sequence=int(number.split("-")[1]),
        status=status, priority=TicketPriority.STANDARD, created_at=created_at or NOW,
    )
    db_session.add(ticket)
    await db_session.commit()
    await db_session.refresh(ticket)
    return ticket


# ══ P1-11 — `return_to_queue` face à une panne Redis ═════════════════════════

@pytest.mark.asyncio
async def test_return_to_queue_nominal_signale_une_file_saine(db_session, mock_queue):
    """Cas nominal : le ticket revient en file, `queue_degraded` est faux."""
    org = await _org(db_session)
    branch = await _branch(db_session, org.id)
    service = await _service(db_session, org.id, branch.id)
    ticket = await _ticket(db_session, org.id, branch.id, service.id, status=TicketStatus.ABSENT)

    result = await TicketService(db_session).return_to_queue(
        ticket.id, org.id, user_id="client-p1"
    )

    assert result["success"] is True
    assert result["queue_degraded"] is False
    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.WAITING


@pytest.mark.asyncio
async def test_return_to_queue_survit_a_une_panne_redis(db_session, mock_queue):
    """
    Redis tombe pendant la remise en file : le client ne doit pas être bloqué.

    Avant le correctif, l'exception remontait en 500 alors que la transition
    WAITING était DÉJÀ commitée : le mobile croyait l'opération échouée et
    réessayait, recevant « transition interdite » — un client dans les limbes,
    présent en base mais absent de la file.

    Désormais la réponse est un succès EXPLICITE avec `queue_degraded=True` :
    le client sait que sa place est enregistrée, et la réparation périodique du
    scheduler le remettra dans la file dès le retour de Redis.
    """
    org = await _org(db_session, "org-p1-down")
    branch = await _branch(db_session, org.id)
    service = await _service(db_session, org.id, branch.id)
    ticket = await _ticket(db_session, org.id, branch.id, service.id,
                           number="A-002", status=TicketStatus.ABSENT)

    with patch(
        "app.services.ticket_service.add_to_queue",
        new_callable=AsyncMock,
        side_effect=RuntimeError("redis down"),
    ):
        result = await TicketService(db_session).return_to_queue(
            ticket.id, org.id, user_id="client-p1"
        )

    assert result["success"] is True, "la remise en file en base a bien eu lieu"
    assert result["queue_degraded"] is True, "le client doit être informé de la dégradation"
    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.WAITING
    assert ticket.priority == TicketPriority.STANDARD, "la pénalité reste appliquée"


# ══ P2-15 — `get_queue_size` ne doit pas annuler un ticket ═══════════════════

@pytest.mark.asyncio
async def test_taille_de_file_indisponible_n_annule_pas_le_ticket(db_session, mock_queue):
    """
    L'ajout réussit, la lecture de la taille échoue : le ticket reste valide.

    Avant le correctif, les deux appels partageaient un `try` : une erreur sur
    `zcard` annulait un ticket pourtant déjà dans le ZSET — entrée fantôme à
    nettoyer, et un client renvoyé chez lui alors que sa place existait.
    """
    org = await _org(db_session, "org-p2-15")
    branch = await _branch(db_session, org.id)
    service = await _service(db_session, org.id, branch.id)

    with patch(
        "app.services.ticket_service.get_queue_size",
        new_callable=AsyncMock,
        side_effect=RuntimeError("zcard failed"),
    ):
        response = await TicketService(db_session).create_ticket(
            CreateTicketRequest(agency_id=branch.id, service_id=service.id),
            user_id="client-p2-15",
            org_id=org.id,
        )

    assert response.status == "waiting", "le ticket ne doit pas être annulé"
    assert response.position == 1
    assert response.total_in_queue == response.position, (
        "la taille est dégradée à la position connue, pas zéro"
    )


@pytest.mark.asyncio
async def test_ajout_en_file_impossible_ne_detruit_plus_le_ticket(db_session, mock_queue):
    """
    Contre-épreuve inversée : si l'AJOUT échoue, le ticket SURVIT, dégradé.

    L'ancien comportement annulait le ticket puis répondait 503. Il protégeait
    l'invariant « pas de WAITING hors file » en détruisant un travail déjà
    accepté en base : le client repartait sans ticket et devait recommencer.

    Depuis la migration 012, l'invariant « un seul ticket actif » est garanti par
    la base, et `rebuild_queue` remet dans la file tout WAITING qui en est absent.
    On garde donc le ticket, on annonce `queue_degraded` et une position inconnue
    — le même arbitrage que `return_to_queue` (P1-11).
    """
    org = await _org(db_session, "org-p2-15-ko")
    branch = await _branch(db_session, org.id)
    service = await _service(db_session, org.id, branch.id)

    with patch(
        "app.services.ticket_service.add_to_queue",
        new_callable=AsyncMock,
        side_effect=RuntimeError("redis down"),
    ):
        ticket = await TicketService(db_session).create_ticket(
            CreateTicketRequest(agency_id=branch.id, service_id=service.id),
            user_id="client-p2-15-ko",
            org_id=org.id,
        )

    assert ticket.status == "waiting", "le ticket n'est plus détruit"
    assert ticket.queue_degraded is True, "le client doit savoir sa position inconnue"
    assert ticket.position == 0

    rows = (await db_session.execute(
        Ticket.__table__.select().where(Ticket.user_id == "client-p2-15-ko")
    )).all()
    assert len(rows) == 1
    assert rows[0].status == TicketStatus.WAITING


# ══ P2-18 — la sonde de readiness doit voir le scheduler ═════════════════════

def test_scheduler_desactive_est_neutre(monkeypatch):
    """`SCHEDULER_ENABLED=false` est un choix d'exploitation, pas une panne."""
    monkeypatch.setattr(health_service.settings, "SCHEDULER_ENABLED", False)
    check = health_service.check_scheduler(_FakeScheduler(running=False))
    assert check["status"] == "disabled"


def test_scheduler_absent_est_neutre():
    """Sans scheduler fourni (tests, CLI), la sonde ne juge pas."""
    assert health_service.check_scheduler(None)["status"] == "not_wired"


def test_boucle_arretee_est_unhealthy():
    """Boucle morte : plus aucune absence automatique, plus d'expiration."""
    check = health_service.check_scheduler(_FakeScheduler(running=False))
    assert check["status"] == "unhealthy"
    assert check["error"] == "loop_stopped"


def test_boucle_silencieuse_est_unhealthy():
    """Boucle vivante mais bloquée sur un appel : détectée par l'âge du dernier tour."""
    stale = time.time() - health_service.SCHEDULER_STALE_AFTER_SECONDS - 1
    check = health_service.check_scheduler(_FakeScheduler(last_run_at=stale))
    assert check["status"] == "unhealthy"
    assert check["error"] == "stalled"


def test_boucle_recente_est_healthy():
    # `time.time()` et non un instant figé au niveau module : la suite complète
    # dure plus de dix minutes, et un `NOW` capturé à l'import vieillirait
    # jusqu'à faire passer un scheduler sain pour bloqué. (Leçon du 15/11 : un
    # test qui dépend de l'heure d'exécution finit toujours par échouer.)
    check = health_service.check_scheduler(_FakeScheduler(last_run_at=time.time() - 2))
    assert check["status"] == "healthy"
    assert check["last_run_age_seconds"] < health_service.SCHEDULER_STALE_AFTER_SECONDS


def test_demarrage_en_cours_n_est_pas_une_panne():
    """Juste après `start()`, `last_run_at` est vide : la référence est `started_at`."""
    check = health_service.check_scheduler(_FakeScheduler(started_at=time.time()))
    assert check["status"] == "starting"


def test_scheduler_qui_n_a_jamais_demarre_est_unhealthy():
    """Le cas réel d'une boucle qui meurt : jamais lancée, ou arrêtée."""
    check = health_service.check_scheduler(QueueScheduler())
    assert check["status"] == "unhealthy"
    assert check["error"] == "loop_stopped"


def test_boucle_lancee_mais_jamais_terminee_est_unhealthy():
    """Lancée il y a longtemps sans avoir bouclé un seul tour : bloquée, pas « en démarrage »."""
    check = health_service.check_scheduler(
        _FakeScheduler(started_at=time.time() - health_service.SCHEDULER_STALE_AFTER_SECONDS - 1)
    )
    assert check["status"] == "unhealthy"
    assert check["error"] == "never_ran"


@pytest.mark.asyncio
async def test_readiness_echoue_si_le_scheduler_est_mort(monkeypatch):
    """
    La sonde interrogée par le monitoring doit basculer en 503.

    Sans ce contrôle, un scheduler mort laissait le service « ready » alors que
    les tickets CALLED ne passaient plus jamais en ABSENT : panne silencieuse
    jusqu'au premier client bloqué au guichet.
    """
    monkeypatch.setattr(
        health_service, "check_database", AsyncMock(return_value={"status": "healthy"})
    )
    monkeypatch.setattr(
        health_service, "check_redis", AsyncMock(return_value={"status": "healthy"})
    )

    ready, checks = await health_service.readiness(scheduler=_FakeScheduler(running=False))
    assert ready is False
    assert checks["scheduler"]["status"] == "unhealthy"

    ready_ok, _ = await health_service.readiness(
        scheduler=_FakeScheduler(last_run_at=time.time() - 1)
    )
    assert ready_ok is True, "un scheduler sain ne doit pas dégrader la readiness"


@pytest.mark.asyncio
async def test_readiness_reste_verte_sans_scheduler(monkeypatch):
    """Les appels sans scheduler (tests, scripts) ne doivent pas casser la sonde."""
    monkeypatch.setattr(
        health_service, "check_database", AsyncMock(return_value={"status": "healthy"})
    )
    monkeypatch.setattr(
        health_service, "check_redis", AsyncMock(return_value={"status": "healthy"})
    )
    ready, checks = await health_service.readiness()
    assert ready is True
    assert checks["scheduler"]["status"] == "not_wired"


# ══ Auto-réparation — le scheduler remet les tickets WAITING en file ═════════

@pytest.mark.asyncio
async def test_la_maintenance_du_scheduler_repare_les_files():
    """
    Le scheduler doit réinsérer les WAITING absents du ZSET (moitié « réparation »
    du correctif P1-11). On vérifie le câblage, pas la mécanique Redis — celle-ci
    est couverte sur vrai Redis par test_rebuild_queue_restores_order_*.
    """
    @asynccontextmanager
    async def factory():
        yield object()

    scheduler = QueueScheduler(session_factory=factory)

    with patch("app.services.queue_scheduler.claim_due_deadlines", AsyncMock(return_value=[])), \
         patch("app.services.queue_scheduler.mark_once", AsyncMock(return_value=True)), \
         patch("app.services.queue_scheduler.TicketService") as service_cls:
        service_cls.return_value.expire_stale_tickets = AsyncMock(return_value={})
        service_cls.return_value.alert_long_waits = AsyncMock(return_value=0)
        service_cls.return_value.repair_open_branches = AsyncMock(return_value=2)

        stats = await scheduler.run_once(now_ts=NOW.timestamp())

    assert stats["repaired"] == 2
    service_cls.return_value.repair_open_branches.assert_awaited_once()
    assert scheduler.last_run_at == NOW.timestamp()


@pytest.mark.asyncio
async def test_la_reparation_se_limite_aux_agences_ouvertes(db_session, mock_queue):
    """
    Le périmètre de réparation est « agences ouvertes », pas « toute la table ».

    Un scan complet de `tickets` toutes les minutes finit par coûter cher : la
    table grandit indéfiniment et `status` n'a pas d'index dédié. On vérifie donc
    que le ticket d'une agence fermée n'est pas traité — il sera de toute façon
    expiré par la règle de fermeture.
    """
    org = await _org(db_session, "org-repair")
    open_branch = await _branch(db_session, org.id, "Centre-ville", is_open=True)
    closed_branch = await _branch(db_session, org.id, "Annexe", is_open=False)
    service_open = await _service(db_session, org.id, open_branch.id)
    service_closed = await _service(db_session, org.id, closed_branch.id)

    await _ticket(db_session, org.id, open_branch.id, service_open.id, "A-001",
                  user_id="client-open")
    await _ticket(db_session, org.id, closed_branch.id, service_closed.id, "A-002",
                  user_id="client-closed")

    with patch(
        "app.services.ticket_service.add_to_queue_if_absent",
        new_callable=AsyncMock,
        return_value=True,
    ) as add_mock:
        restored = await TicketService(db_session).repair_open_branches()

    assert restored == 1
    assert add_mock.await_count == 1
    assert add_mock.await_args.args[1] == open_branch.id


# ══ Régression P1-12 — le total de pagination de l'historique ════════════════

@pytest.mark.asyncio
async def test_history_garde_le_total_de_pagination(db_session, mock_queue):
    """
    `total` de `TicketHistoryResponse` doit rester le nombre de tickets du client.

    Le correctif P1-12 avait réutilisé la variable `total` pour la taille de la
    file : après la boucle, `has_next` et `total` décrivaient donc la file du
    dernier ticket WAITING (souvent 1), plus l'historique. La pagination mobile
    s'arrêtait après la première page.
    """
    org = await _org(db_session, "org-hist")
    branch = await _branch(db_session, org.id)
    service = await _service(db_session, org.id, branch.id)
    # Un client ne peut avoir qu'UN ticket actif (index unique de la migration
    # 012) : les deux autres sont clos, ce qui est de toute façon le cas réel
    # d'un historique. Le ticket WAITING reste nécessaire : c'est sa branche qui
    # écrasait `total` avec la taille de la file, le défaut que ce test couvre.
    await _ticket(db_session, org.id, branch.id, service.id,
                  number="A-001", user_id="client-hist")
    for i in range(2, 4):
        await _ticket(db_session, org.id, branch.id, service.id,
                      number=f"A-{i:03d}", user_id="client-hist",
                      status=TicketStatus.DONE)

    # La file simulée annonce 1 ticket : si `total` était écrasé, il vaudrait 1.
    history = await TicketService(db_session).get_history("client-hist", page=1)

    assert history.total == 3
    assert len(history.items) == 3
    assert history.has_next is False


# ══ Sondes de santé : les moniteurs externes sondent en HEAD ═════════════════

def test_les_sondes_de_sante_acceptent_head():
    """
    UptimeRobot et la plupart des moniteurs sondent d'abord en `HEAD`. FastAPI
    n'ajoute pas `HEAD` aux routes `GET` : une sonde déclarée `@router.get`
    répond 405 et le moniteur bascule DOWN alors que le service va bien.

    L'invariant est vérifié sur la table de routage, sans requête HTTP — donc il
    tient aussi dans les environnements sans Redis. Il couvre désormais
    `/health/ready` et `/health/status`, qui sont les deux sondes qu'on veut
    réellement surveiller (une panne de base ou un scheduler mort s'y voient,
    alors que `/health` ne teste rien par construction).
    """
    from app.main import app

    attendues = {"/health", "/health/live", "/health/ready", "/health/status"}
    methodes = {}
    for route in app.routes:
        chemin = getattr(route, "path", None)
        if chemin in attendues:
            methodes.setdefault(chemin, set()).update(getattr(route, "methods", set()) or set())

    for chemin in attendues:
        assert "HEAD" in methodes.get(chemin, set()), f"{chemin} refuse HEAD : {methodes.get(chemin)}"
        assert "GET" in methodes[chemin], chemin
