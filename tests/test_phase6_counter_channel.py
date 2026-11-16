"""
Constat 2 de l'agent desktop — les trois défauts du canal guichet.

Les trois ont été confirmés en lisant le code avant d'écrire une ligne :

  1. `websocket_counter` lisait `user.org_id` seul. Un `super_admin` (org_id NULL
     par construction) ne pouvait donc s'abonner au flux d'AUCUN guichet, et un
     agent rattaché à une organisation dont `User.org_id` ne reflétait pas le
     rattachement actif était refusé alors qu'il était légitime.

  2. Aucune route `/recall` n'existait. L'agent qui venait d'appeler un client
     resté dehors n'avait qu'une option : le déclarer ABSENT — c'est-à-dire le
     renvoyer derrière tout le monde pour un problème d'audition. Le poste
     desktop avait donc masqué son bouton « R » hors mode démonstration.

  3. `counter_connections` était alimenté à la connexion mais aucun événement ne
     le parcourait jamais. Les écrans de guichet n'apprenaient rien en temps
     réel, d'où le sondage HTTP toutes les 7 secondes.

Ce fichier teste les trois sans ouvrir de socket : la décision d'autorisation a
été sortie du handler WebSocket justement pour être vérifiable ici, et le routage
des événements se teste sur le manager seul.
"""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.config import settings
from app.core.redis import DEADLINE_ABSENT, DEADLINE_WARNING
from app.models.audit_log import AuditAction, AuditLog
from app.models.counter import Counter
from app.models.employee import Employee, EmployeeStatus
from app.models.organization import Organization
from app.models.ticket import Ticket, TicketStatus
from app.models.user import User, UserRole
from app.routers.tickets import _authorize_counter_stream
from app.schemas.ticket import CreateTicketRequest
from app.services.ticket_service import TicketService
from app.websocket.events import EventType
from app.websocket.queue_ws import QueueWebSocketManager
from tests.test_queue_atomicity import _make_counter
from tests.test_ticket_service import (
    _create_test_agency,
    _create_test_org,
    _create_test_service,
)


# ── Fabriques ────────────────────────────────────────────────────────────────

async def _org(db, name: str) -> Organization:
    org = Organization(name=name, slug=f"org-{uuid.uuid4().hex[:8]}", sector="bank")
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return org


async def _user(db, role: UserRole, *, org_id: str | None = None,
                is_active: bool = True) -> User:
    user = User(
        phone=f"+257{uuid.uuid4().int % 10**8:08d}",
        role=role,
        org_id=org_id,
        is_active=is_active,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _employee(db, user_id: str, org_id: str, *, role: UserRole = UserRole.AGENT,
                    counter_id: str | None = None, agency_id: str | None = None,
                    status: EmployeeStatus = EmployeeStatus.ACTIVE) -> Employee:
    employee = Employee(
        org_id=org_id, user_id=user_id, role=role,
        counter_id=counter_id, agency_id=agency_id, status=status,
    )
    db.add(employee)
    await db.commit()
    await db.refresh(employee)
    return employee


class _Canaux:
    """Ce que le test observe : les événements publiés et les échéances posées."""

    def __init__(self, publish: AsyncMock, deadline: AsyncMock) -> None:
        self.publish = publish
        self.deadline = deadline

    def events(self, event_type: str) -> list[dict]:
        """Tous les événements publiés d'un type donné."""
        return [
            call.args[1] for call in self.publish.call_args_list
            if len(call.args) > 1 and call.args[1].get("type") == event_type
        ]

    def deadlines(self, kind: str) -> list[float]:
        return [call.args[3] for call in self.deadline.call_args_list if call.args[0] == kind]


# Les dépendances Redis de TicketService sont remplacées au point où le service
# les résout. `mock_redis` reste nécessaire : le verrou de création (`SET NX`) et
# sa libération passent par le client Redis, pas par ces helpers.
@pytest.fixture
def canaux(mock_redis):
    """
    TicketService isolé de Redis : la publication et les échéances sont observables.

    Depuis la migration 014, les événements de guichet passent par l'Outbox :
    `_publish_counter_activity` écrit une ligne puis appelle le `publish_event`
    de `app.services.outbox`. On patche donc les DEUX points de diffusion avec le
    MÊME mock, pour que `call_args_list` contienne tous les événements dans
    l'ordre — sinon le test ne verrait que la moitié de ce qui est publié.
    """
    with patch("app.services.ticket_service.publish_event", new_callable=AsyncMock) as publish, \
         patch("app.services.outbox.publish_event", new=publish), \
         patch("app.services.ticket_service.set_deadline", new_callable=AsyncMock) as deadline, \
         patch("app.services.ticket_service.clear_deadlines", new_callable=AsyncMock), \
         patch("app.services.ticket_service.add_to_queue", new_callable=AsyncMock, return_value=1), \
         patch("app.services.ticket_service.add_to_queue_if_absent", new_callable=AsyncMock, return_value=False), \
         patch("app.services.ticket_service.get_queue_size", new_callable=AsyncMock, return_value=0), \
         patch("app.services.ticket_service.get_queue_position", new_callable=AsyncMock, return_value=1), \
         patch("app.services.ticket_service.remove_from_queue", new_callable=AsyncMock), \
         patch("app.services.ticket_service.get_queue_snapshot", new_callable=AsyncMock, return_value=[]):
        yield _Canaux(publish, deadline)


@pytest.fixture
def canaux_muet(mock_redis):
    """
    Redis injoignable, tel que le service le voit réellement.

    `publish_event` avale ses propres exceptions et retourne `False` : c'est son
    contrat (`app/websocket/publisher.py`). Simuler un `raise` ici testerait le
    mock, pas le service — on simule donc le vrai mode de défaillance. C'est
    aussi ce mode que l'Outbox doit savoir rattraper (migration 014) : la ligne
    reste en attente au lieu de disparaître.
    """
    with patch("app.services.ticket_service.publish_event", new_callable=AsyncMock,
               return_value=False) as publish, \
         patch("app.services.outbox.publish_event", new=publish), \
         patch("app.services.ticket_service.set_deadline", new_callable=AsyncMock), \
         patch("app.services.ticket_service.clear_deadlines", new_callable=AsyncMock), \
         patch("app.services.ticket_service.add_to_queue", new_callable=AsyncMock, return_value=1), \
         patch("app.services.ticket_service.add_to_queue_if_absent", new_callable=AsyncMock, return_value=False), \
         patch("app.services.ticket_service.get_queue_size", new_callable=AsyncMock, return_value=0), \
         patch("app.services.ticket_service.get_queue_position", new_callable=AsyncMock, return_value=1), \
         patch("app.services.ticket_service.remove_from_queue", new_callable=AsyncMock), \
         patch("app.services.ticket_service.get_queue_snapshot", new_callable=AsyncMock, return_value=[]):
        yield


@pytest.fixture
def canaux_en_panne(mock_redis):
    """
    Redis qui lève — l'autre mode de défaillance.

    Il sert à vérifier les garde-fous locaux (`try/except` autour de la
    publication) : si un jour `publish_event` laissait remonter une exception,
    le chemin métier ne doit pas en mourir. Depuis l'Outbox, l'événement est de
    toute façon déjà en base — l'exception ne peut plus le perdre.
    """
    with patch("app.services.ticket_service.publish_event", new_callable=AsyncMock,
               side_effect=RuntimeError("Redis injoignable")) as publish, \
         patch("app.services.outbox.publish_event", new=publish), \
         patch("app.services.ticket_service.set_deadline", new_callable=AsyncMock), \
         patch("app.services.ticket_service.clear_deadlines", new_callable=AsyncMock), \
         patch("app.services.ticket_service.add_to_queue", new_callable=AsyncMock, return_value=1), \
         patch("app.services.ticket_service.add_to_queue_if_absent", new_callable=AsyncMock, return_value=False), \
         patch("app.services.ticket_service.get_queue_size", new_callable=AsyncMock, return_value=0), \
         patch("app.services.ticket_service.get_queue_position", new_callable=AsyncMock, return_value=1), \
         patch("app.services.ticket_service.remove_from_queue", new_callable=AsyncMock), \
         patch("app.services.ticket_service.get_queue_snapshot", new_callable=AsyncMock, return_value=[]):
        yield


async def _scene_guichets(db) -> dict:
    """
    Deux organisations, un guichet par organisation, plus un service côté A.

    Deux organisations sont nécessaires : le cœur du constat 1 est de distinguer
    « pas d'organisation » de « mauvaise organisation ».
    """
    org_a = await _org(db, "Banque A")
    org_b = await _org(db, "Banque B")
    agency_a = await _create_test_agency(db, org_a.id)
    agency_b = await _create_test_agency(db, org_b.id)
    service_a = await _create_test_service(db, agency_a.id, org_a.id)
    counter_a = await _make_counter(db, org_a.id, agency_a.id, name="Guichet A1")
    counter_b = await _make_counter(db, org_b.id, agency_b.id, name="Guichet B1")
    return {
        "org_a": org_a, "org_b": org_b,
        "agency_a": agency_a, "agency_b": agency_b,
        "service_a": service_a,
        "counter_a": counter_a, "counter_b": counter_b,
    }


async def _agent_au_guichet(db, scene: dict) -> User:
    """Agent rattaché au guichet A1 par un `Employee` actif."""
    agent = await _user(db, UserRole.AGENT, org_id=scene["org_a"].id)
    await _employee(db, agent.id, scene["org_a"].id,
                    counter_id=scene["counter_a"].id, agency_id=scene["agency_a"].id)
    return agent


async def _superviseur(db, scene: dict) -> User:
    """
    Superviseur de l'agence : agit sur tout ticket de sa branch, sans être
    affecté à un guichet. Sert à isoler les gardes qui passent APRÈS le contrôle
    d'affectation de l'agent.
    """
    superviseur = await _user(db, UserRole.SUPERVISOR, org_id=scene["org_a"].id)
    await _employee(db, superviseur.id, scene["org_a"].id,
                    role=UserRole.SUPERVISOR, agency_id=scene["agency_a"].id)
    return superviseur


async def _ticket_appele(db, service: TicketService, scene: dict, *,
                         counter: Counter | None = None) -> Ticket:
    """Ticket créé puis amené en CALLED au guichet demandé."""
    counter = counter or scene["counter_a"]
    created = await service.create_ticket(
        CreateTicketRequest(agency_id=scene["agency_a"].id, service_id=scene["service_a"].id),
        "client-1",
        scene["org_a"].id,
    )
    ticket = (await db.execute(
        select(Ticket).where(Ticket.id == created.id).execution_options(populate_existing=True)
    )).scalar_one()
    ticket.status = TicketStatus.CALLED
    ticket.counter_id = counter.id
    ticket.counter_name = counter.name
    ticket.called_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(ticket)
    return ticket


# ══════════════════════════════════════════════════════════════════════════════
# Constat 1 — autorisation du WebSocket guichet
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_super_admin_sans_org_id_peut_ecouter_un_guichet(db_session):
    """
    LE bug d'origine : `super_admin` a `org_id = NULL` par construction
    (`bootstrap_service`), et le handler fermait la connexion en 4003 « Agent
    sans organisation ». L'équipe Tonde ne pouvait donc diagnostiquer aucun
    guichet en temps réel — sur aucune organisation.
    """
    scene = await _scene_guichets(db_session)
    admin = await _user(db_session, UserRole.SUPER_ADMIN, org_id=None)
    assert admin.org_id is None, "prémisse : un super_admin n'a pas d'organisation"

    counter, refusal = await _authorize_counter_stream(db_session, admin, scene["counter_a"].id)

    assert refusal is None
    assert counter is not None and counter.id == scene["counter_a"].id


@pytest.mark.asyncio
async def test_super_admin_peut_ecouter_un_guichet_de_n_importe_quelle_orgue(db_session):
    """Le périmètre d'un super_admin est transverse : l'organisation B aussi."""
    scene = await _scene_guichets(db_session)
    admin = await _user(db_session, UserRole.SUPER_ADMIN, org_id=None)

    _, refusal = await _authorize_counter_stream(db_session, admin, scene["counter_b"].id)

    assert refusal is None


@pytest.mark.asyncio
async def test_agent_sans_org_id_mais_avec_rattachement_actif_est_accepte(db_session):
    """
    `User.org_id` peut être NULL alors que le rattachement `Employee` existe :
    c'est l'état d'un compte créé par le flux d'invitation avant que `org_id`
    soit renseigné. Le contrôle doit lire le rattachement, pas le champ hérité.
    """
    scene = await _scene_guichets(db_session)
    agent = await _user(db_session, UserRole.AGENT, org_id=None)
    await _employee(db_session, agent.id, scene["org_a"].id,
                    counter_id=scene["counter_a"].id, agency_id=scene["agency_a"].id)

    _, refusal = await _authorize_counter_stream(db_session, agent, scene["counter_a"].id)

    assert refusal is None


@pytest.mark.asyncio
async def test_agent_dont_user_org_id_designe_une_autre_orgue_reste_accepte(db_session):
    """
    Cas multi-organisations : `deactivate` ne réécrit pas `User.org_id` quand un
    autre rattachement subsiste (P0-5). Un agent peut donc avoir un `org_id`
    périmé et un rattachement actif ailleurs. Se fier au seul `org_id` le
    refusait à son propre guichet.
    """
    scene = await _scene_guichets(db_session)
    agent = await _user(db_session, UserRole.AGENT, org_id=scene["org_b"].id)
    await _employee(db_session, agent.id, scene["org_a"].id,
                    counter_id=scene["counter_a"].id, agency_id=scene["agency_a"].id)

    counter, refusal = await _authorize_counter_stream(db_session, agent, scene["counter_a"].id)

    assert refusal is None
    assert counter.id == scene["counter_a"].id


@pytest.mark.asyncio
async def test_agent_non_affecte_au_guichet_reste_refuse(db_session):
    """
    Le contrôle P3-10 doit survivre au correctif : un agent de l'agence ne doit
    pas écouter le guichet d'un collègue (il verrait ses clients appelés).
    """
    scene = await _scene_guichets(db_session)
    agent = await _user(db_session, UserRole.AGENT, org_id=scene["org_a"].id)
    autre_guichet = await _make_counter(
        db_session, scene["org_a"].id, scene["agency_a"].id, name="Guichet A2"
    )
    await _employee(db_session, agent.id, scene["org_a"].id,
                    counter_id=autre_guichet.id, agency_id=scene["agency_a"].id)

    _, refusal = await _authorize_counter_stream(db_session, agent, scene["counter_a"].id)

    assert refusal == (4003, "Vous n'êtes pas affecté à ce guichet")


@pytest.mark.asyncio
async def test_rattachement_inactif_ne_donne_pas_acces(db_session):
    """Un `Employee` en congé ou suspendu n'ouvre pas le flux du guichet."""
    scene = await _scene_guichets(db_session)
    agent = await _user(db_session, UserRole.AGENT, org_id=scene["org_a"].id)
    await _employee(db_session, agent.id, scene["org_a"].id,
                    counter_id=scene["counter_a"].id, agency_id=scene["agency_a"].id,
                    status=EmployeeStatus.SUSPENDED)

    _, refusal = await _authorize_counter_stream(db_session, agent, scene["counter_a"].id)

    assert refusal == (4003, "Vous n'êtes pas affecté à ce guichet")


@pytest.mark.asyncio
async def test_guichet_d_une_orgue_etrangere_reste_introuvable(db_session):
    """
    4004 et non 403 : on ne révèle pas l'existence d'un guichet d'une autre
    organisation. C'est la même règle que le REST (`_get_ticket_for_staff`).
    """
    scene = await _scene_guichets(db_session)
    agent = await _agent_au_guichet(db_session, scene)

    _, refusal = await _authorize_counter_stream(db_session, agent, scene["counter_b"].id)

    assert refusal == (4004, "Guichet introuvable dans votre organisation")


@pytest.mark.asyncio
async def test_client_ne_peut_pas_ecouter_un_guichet(db_session):
    """Le flux d'un guichet n'est pas public : un CLIENT est refusé d'entrée."""
    scene = await _scene_guichets(db_session)
    client = await _user(db_session, UserRole.CLIENT, org_id=None)

    _, refusal = await _authorize_counter_stream(db_session, client, scene["counter_a"].id)

    assert refusal == (4003, "Accès réservé aux agents")


@pytest.mark.asyncio
async def test_token_pointant_un_compte_disparu_est_refuse(db_session):
    """Jeton valide mais compte supprimé entre-temps : `user is None`."""
    scene = await _scene_guichets(db_session)

    _, refusal = await _authorize_counter_stream(db_session, None, scene["counter_a"].id)

    assert refusal == (4003, "Accès réservé aux agents")


@pytest.mark.asyncio
async def test_compte_desactive_est_refuse(db_session):
    """Un compte désactivé ne garde pas un canal ouvert parce que son jeton est valide."""
    scene = await _scene_guichets(db_session)
    agent = await _user(db_session, UserRole.AGENT, org_id=scene["org_a"].id, is_active=False)

    _, refusal = await _authorize_counter_stream(db_session, agent, scene["counter_a"].id)

    assert refusal == (4003, "Accès réservé aux agents")


@pytest.mark.asyncio
async def test_staff_sans_aucun_rattachement_est_refuse_explicitement(db_session):
    """
    Un compte staff sans organisation ni rattachement est mal configuré : le
    message le dit, au lieu de laisser croire à un guichet inexistant.
    """
    scene = await _scene_guichets(db_session)
    agent = await _user(db_session, UserRole.AGENT, org_id=None)

    _, refusal = await _authorize_counter_stream(db_session, agent, scene["counter_a"].id)

    assert refusal == (4003, "Compte staff sans rattachement à une organisation")


@pytest.mark.asyncio
async def test_superviseur_ouvre_un_guichet_sans_y_etre_affecte(db_session):
    """
    SUPERVISOR et au-dessus surveillent la salle : ils n'ont pas besoin d'être
    affectés à un guichet précis, seulement d'être dans l'organisation.
    """
    scene = await _scene_guichets(db_session)
    superviseur = await _user(db_session, UserRole.SUPERVISOR, org_id=scene["org_a"].id)
    await _employee(db_session, superviseur.id, scene["org_a"].id,
                    role=UserRole.SUPERVISOR, agency_id=scene["agency_a"].id)

    _, refusal = await _authorize_counter_stream(db_session, superviseur, scene["counter_a"].id)

    assert refusal is None


@pytest.mark.asyncio
async def test_get_staff_org_ids_reunit_user_org_id_et_rattachements(db_session):
    """
    L'helper qui porte le correctif : union de `User.org_id` et des `Employee`
    actifs. Il est la source unique du contrôle WebSocket.
    """
    from app.core.access import get_staff_org_ids

    scene = await _scene_guichets(db_session)
    org_c = await _org(db_session, "Banque C")
    agent = await _user(db_session, UserRole.AGENT, org_id=scene["org_a"].id)
    await _employee(db_session, agent.id, scene["org_b"].id, agency_id=scene["agency_b"].id)
    await _employee(db_session, agent.id, org_c.id)
    # Rattachement inactif : ne doit pas entrer dans l'ensemble.
    await _employee(db_session, agent.id, scene["org_a"].id,
                    status=EmployeeStatus.INACTIVE)

    org_ids = await get_staff_org_ids(db_session, agent)

    assert org_ids == {scene["org_a"].id, scene["org_b"].id, org_c.id}


# ══════════════════════════════════════════════════════════════════════════════
# Constat 2 — relance d'un ticket appelé
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_relance_rediffuse_l_annonce_et_remet_le_compte_a_rebours(db_session, canaux):
    """
    Le cœur du besoin : le client n'a pas entendu. On rediffuse (salle +
    téléphone) ET on repart de zéro sur le délai ABSENT — sinon le ticket passe
    ABSENT pendant que le client arrive, et la relance ne sert à rien.
    """
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    agent = await _agent_au_guichet(db_session, scene)

    ticket = await _ticket_appele(db_session, service, scene)
    canaux.publish.reset_mock()
    canaux.deadline.reset_mock()

    avant = datetime.now(timezone.utc).timestamp()
    result = await service.recall(ticket.id, agent)
    apres = datetime.now(timezone.utc).timestamp()

    assert result["success"] is True
    assert result["ticket_number"] == ticket.number
    assert result["counter_name"] == "Guichet A1"
    assert result["recall_count"] == 1
    assert result["recalls_left"] == settings.MAX_RECALLS_PER_TICKET - 1
    assert result["timeout_seconds"] == settings.ABSENT_TIMEOUT_SECONDS

    # 1. Le client : téléphone puis SMS/push.
    your_turn = canaux.events(EventType.YOUR_TURN.value)
    assert len(your_turn) == 1
    assert your_turn[0]["recall"] is True
    assert your_turn[0]["ticket_id"] == ticket.id
    assert your_turn[0]["counter_name"] == "Guichet A1"
    assert your_turn[0]["timeout_seconds"] == settings.ABSENT_TIMEOUT_SECONDS

    # 2. La salle : l'écran d'affichage rediffuse le numéro.
    called = canaux.events(EventType.TICKET_CALLED.value)
    assert len(called) == 1
    assert called[0]["recall"] is True
    assert called[0]["called_number"] == ticket.number
    assert called[0]["agency_id"] == scene["agency_a"].id

    # 3. Les écrans de guichet de l'agence.
    guichet = canaux.events(EventType.COUNTER_QUEUE_CHANGED.value)
    assert len(guichet) == 1
    assert guichet[0]["reason"] == "ticket_recalled"
    assert guichet[0]["ticket_number"] == ticket.number
    assert guichet[0]["counter_id"] == scene["counter_a"].id

    # 4. Le compte à rebours est ancré sur MAINTENANT, pas sur l'appel d'origine.
    absents = canaux.deadlines(DEADLINE_ABSENT)
    assert len(absents) == 1
    assert avant + settings.ABSENT_TIMEOUT_SECONDS <= absents[0] <= apres + settings.ABSENT_TIMEOUT_SECONDS
    assert canaux.deadlines(DEADLINE_WARNING), "l'avertissement doit être reprogrammé aussi"


@pytest.mark.asyncio
async def test_relance_laisse_une_trace_d_audit(db_session, canaux):
    """
    Chaque relance coûte un SMS. Le journal d'audit est la seule trace durable
    de « qui a relancé ce client, combien de fois » — et c'est lui qui porte le
    plafond, pas un compteur Redis qui disparaît au redémarrage.
    """
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    agent = await _agent_au_guichet(db_session, scene)

    ticket = await _ticket_appele(db_session, service, scene)
    await service.recall(ticket.id, agent)

    rows = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == AuditAction.TICKET_RECALLED)
    )).scalars().all()

    assert len(rows) == 1
    assert rows[0].actor_id == agent.id
    assert rows[0].target_id == ticket.id
    assert rows[0].org_id == scene["org_a"].id
    assert rows[0].extra_data["recall_number"] == 1
    assert rows[0].extra_data["counter_id"] == scene["counter_a"].id
    assert rows[0].extra_data["ticket_number"] == ticket.number


@pytest.mark.asyncio
async def test_relance_d_un_ticket_en_attente_est_refusee(db_session, canaux):
    """
    On ne relance pas une annonce qui n'a jamais eu lieu : le ticket doit être CALLED.

    L'acteur est un superviseur, pas l'agent : un agent se heurterait d'abord au
    contrôle « ce ticket n'est pas à votre guichet » (403), qui masquerait le vrai
    motif. Le superviseur, lui, atteint le contrôle de statut — c'est donc le cas
    qui teste réellement la garde.
    """
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    superviseur = await _superviseur(db_session, scene)

    created = await service.create_ticket(
        CreateTicketRequest(agency_id=scene["agency_a"].id, service_id=scene["service_a"].id),
        "client-1",
        scene["org_a"].id,
    )

    with pytest.raises(HTTPException) as exc:
        await service.recall(created.id, superviseur)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "TICKET_NOT_CALLED"
    assert "waiting" in exc.value.detail["message"]


@pytest.mark.asyncio
async def test_relance_plafonnee(db_session, canaux):
    """
    Chaque relance envoie un vrai SMS. Sans plafond, un agent qui s'acharne sur
    le bouton fait exploser la facture et harcèle un client qui a quitté la
    salle. `MAX_RECALLS_PER_TICKET` relances, puis 409 explicite qui indique la
    bonne action (déclarer absent).
    """
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    agent = await _agent_au_guichet(db_session, scene)

    ticket = await _ticket_appele(db_session, service, scene)

    for numero in range(1, settings.MAX_RECALLS_PER_TICKET + 1):
        result = await service.recall(ticket.id, agent)
        assert result["recall_count"] == numero

    with pytest.raises(HTTPException) as exc:
        await service.recall(ticket.id, agent)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "RECALL_LIMIT_REACHED"
    assert exc.value.detail["recall_count"] == settings.MAX_RECALLS_PER_TICKET
    assert exc.value.detail["max_recalls"] == settings.MAX_RECALLS_PER_TICKET


@pytest.mark.asyncio
async def test_relance_d_un_ticket_d_un_autre_guichet_est_refusee(db_session, canaux):
    """
    Un agent ne relance que ses propres appels : relancer l'annonce d'un
    collègue depuis son poste enverrait le client au mauvais endroit.
    """
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    await _agent_au_guichet(db_session, scene)

    counter_2 = await _make_counter(
        db_session, scene["org_a"].id, scene["agency_a"].id, name="Guichet A2"
    )
    agent_2 = await _user(db_session, UserRole.AGENT, org_id=scene["org_a"].id)
    await _employee(db_session, agent_2.id, scene["org_a"].id,
                    counter_id=counter_2.id, agency_id=scene["agency_a"].id)

    ticket = await _ticket_appele(db_session, service, scene)  # au guichet A1

    with pytest.raises(HTTPException) as exc:
        await service.recall(ticket.id, agent_2)

    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "TICKET_NOT_YOURS"


@pytest.mark.asyncio
async def test_relance_d_un_ticket_sans_guichet_est_refusee(db_session, canaux):
    """
    Un ticket CALLED sans guichet est un état incohérent (aucun appel n'a pu le
    produire). On refuse avec un code dédié plutôt que de lever une 500 sur le
    `counter.name` d'un objet `None`.
    """
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    superviseur = await _superviseur(db_session, scene)

    ticket = await _ticket_appele(db_session, service, scene)
    ticket.counter_id = None
    ticket.counter_name = None
    await db_session.commit()

    with pytest.raises(HTTPException) as exc:
        await service.recall(ticket.id, superviseur)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "TICKET_WITHOUT_COUNTER"


@pytest.mark.asyncio
async def test_un_sms_qui_echoue_n_annule_pas_la_relance(db_session, canaux):
    """
    L'annonce sonore et visuelle est le canal principal ; le SMS est un
    complément payant. Une passerelle en panne ne doit pas empêcher la salle
    d'entendre le numéro — même règle que `publish_event`.
    """
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    agent = await _agent_au_guichet(db_session, scene)

    ticket = await _ticket_appele(db_session, service, scene)
    canaux.publish.reset_mock()

    with patch(
        "app.services.notification_service.NotificationService.notify_ticket_called",
        new_callable=AsyncMock,
        side_effect=RuntimeError("passerelle SMS indisponible"),
    ):
        result = await service.recall(ticket.id, agent)

    assert result["success"] is True
    assert len(canaux.events(EventType.TICKET_CALLED.value)) == 1


@pytest.mark.asyncio
async def test_une_panne_du_temps_reel_n_empeche_pas_la_relance(db_session, canaux_muet):
    """
    Le temps réel accélère, il ne conditionne pas. Si Redis est injoignable, la
    relance doit quand même aboutir — l'écran de salle se resynchronisera.
    """
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    agent = await _agent_au_guichet(db_session, scene)

    ticket = await _ticket_appele(db_session, service, scene)
    result = await service.recall(ticket.id, agent)

    assert result["success"] is True
    assert result["recall_count"] == 1


def test_la_route_de_relance_existe_et_est_un_post():
    """
    Le poste desktop avait masqué son bouton « R » faute d'endpoint. On vérifie
    la route sur la table de routage — donc sans Redis, sans HTTP.
    """
    from app.main import app

    chemins = {}
    for route in app.routes:
        chemin = getattr(route, "path", None)
        if chemin:
            chemins.setdefault(chemin, set()).update(getattr(route, "methods", set()) or set())

    assert "POST" in chemins.get("/api/v1/tickets/{ticket_id}/recall", set())


def test_le_plafond_de_relance_est_strictement_positif():
    """Un plafond à 0 supprimerait la fonctionnalité ; il doit rester configurable."""
    assert settings.MAX_RECALLS_PER_TICKET >= 1


# ══════════════════════════════════════════════════════════════════════════════
# Constat 3 — canal WebSocket guichet actif
# ══════════════════════════════════════════════════════════════════════════════

class _Ecran:
    """WebSocket minimal : accepte et garde trace de ce qu'il reçoit."""

    def __init__(self, *, mort: bool = False) -> None:
        self.mort = mort
        self.recus: list[dict] = []

    async def accept(self) -> None:
        pass

    async def send_text(self, data: str) -> None:
        if self.mort:
            raise RuntimeError("connexion fermée")
        self.recus.append(json.loads(data))


@pytest.mark.asyncio
async def test_un_changement_de_file_atteint_tous_les_guichets_de_l_agence():
    """
    Le chaînon qui manquait : `counter_connections` était alimenté à la connexion
    mais aucun événement ne le parcourait. Un guichet peut servir n'importe quel
    service de son agence : le changement de file le concerne tous.
    """
    manager = QueueWebSocketManager()
    ecran_1, ecran_2, ecran_ailleurs = _Ecran(), _Ecran(), _Ecran()

    await manager.connect_counter(ecran_1, "counter-1", agency_id="agency-A")
    await manager.connect_counter(ecran_2, "counter-2", agency_id="agency-A")
    await manager.connect_counter(ecran_ailleurs, "counter-3", agency_id="agency-B")

    evenement = {"type": EventType.COUNTER_QUEUE_CHANGED.value, "reason": "ticket_created"}
    await manager.send_to_agency_counters("agency-A", evenement)

    assert [e["reason"] for e in ecran_1.recus] == ["ticket_created"]
    assert [e["reason"] for e in ecran_2.recus] == ["ticket_created"]
    assert ecran_ailleurs.recus == [], "une autre agence ne doit rien recevoir"


@pytest.mark.asyncio
async def test_un_ecran_mort_n_affame_pas_les_autres():
    """
    Chaque guichet reçoit sa propre copie : un poste éteint ne doit pas empêcher
    le voisin d'être prévenu. C'est la raison d'être de la boucle par guichet
    plutôt que d'un envoi groupé.
    """
    manager = QueueWebSocketManager()
    vivant, mort = _Ecran(), _Ecran(mort=True)

    await manager.connect_counter(vivant, "counter-1", agency_id="agency-A")
    await manager.connect_counter(mort, "counter-2", agency_id="agency-A")

    await manager.send_to_agency_counters("agency-A", {"type": "counter_queue_changed"})

    assert len(vivant.recus) == 1
    # La connexion morte a été retirée de l'index.
    assert "counter-2" not in manager.counter_connections


@pytest.mark.asyncio
async def test_deux_ecrans_sur_un_meme_guichet_sont_servis_separement():
    """Un guichet peut avoir deux écrans ; chacun reçoit une copie."""
    manager = QueueWebSocketManager()
    ecran_a, ecran_b = _Ecran(), _Ecran()

    await manager.connect_counter(ecran_a, "counter-1", agency_id="agency-A")
    await manager.connect_counter(ecran_b, "counter-1", agency_id="agency-A")

    await manager.send_to_agency_counters("agency-A", {"type": "counter_queue_changed"})

    assert len(ecran_a.recus) == 1 and len(ecran_b.recus) == 1


@pytest.mark.asyncio
async def test_l_index_d_agence_ne_survit_pas_au_dernier_ecran():
    """
    Sinon `counter_agency` pointerait indéfiniment vers un guichet dont plus
    personne n'écoute, et `send_to_agency_counters` parcourrait une liste morte.
    """
    manager = QueueWebSocketManager()
    ecran_a, ecran_b = _Ecran(), _Ecran()

    await manager.connect_counter(ecran_a, "counter-1", agency_id="agency-A")
    await manager.connect_counter(ecran_b, "counter-1", agency_id="agency-A")

    manager.disconnect_counter("counter-1", ecran_a)
    assert manager.counter_agency["counter-1"] == "agency-A", "il reste un écran"

    manager.disconnect_counter("counter-1", ecran_b)
    assert "counter-1" not in manager.counter_agency
    assert "counter-1" not in manager.counter_connections


@pytest.mark.asyncio
async def test_un_guichet_connecte_sans_agence_ne_recoit_rien():
    """
    Comportement documenté : `agency_id` est la clé de routage. Sans elle, le
    guichet est enregistré mais ne peut pas être ciblé — c'est pourquoi le
    handler la passe toujours.
    """
    manager = QueueWebSocketManager()
    ecran = _Ecran()
    await manager.connect_counter(ecran, "counter-1")  # pas d'agency_id

    await manager.send_to_agency_counters("agency-A", {"type": "counter_queue_changed"})

    assert ecran.recus == []
    assert "counter-1" in manager.counter_connections


@pytest.mark.asyncio
async def test_dispatch_route_les_evenements_de_guichet():
    """`_dispatch_event` doit router `counter_queue_changed` vers les guichets."""
    manager = QueueWebSocketManager()

    with patch.object(manager, "send_to_agency_counters", new_callable=AsyncMock) as mock_envoi:
        await manager._dispatch_event({
            "type": EventType.COUNTER_QUEUE_CHANGED.value,
            "agency_id": "agency-A",
            "reason": "ticket_created",
        })

    mock_envoi.assert_awaited_once_with("agency-A", {
        "type": EventType.COUNTER_QUEUE_CHANGED.value,
        "agency_id": "agency-A",
        "reason": "ticket_created",
    })


@pytest.mark.asyncio
async def test_une_creation_de_ticket_previent_les_guichets(db_session, canaux):
    """
    Fin du sondage à 7 secondes : une création dans la file de l'agence pousse un
    événement. Le message reste pauvre (ce qui a changé, pas l'état complet) —
    c'est ce qui évite d'inventer ici un format que le desktop devra suivre.
    """
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)

    created = await service.create_ticket(
        CreateTicketRequest(agency_id=scene["agency_a"].id, service_id=scene["service_a"].id),
        "client-1",
        scene["org_a"].id,
    )

    evenements = canaux.events(EventType.COUNTER_QUEUE_CHANGED.value)
    assert len(evenements) == 1
    assert evenements[0]["reason"] == "ticket_created"
    assert evenements[0]["agency_id"] == scene["agency_a"].id
    assert evenements[0]["service_id"] == scene["service_a"].id
    assert evenements[0]["ticket_number"] == created.number
    assert evenements[0]["status"] == "waiting"


@pytest.mark.asyncio
async def test_chaque_transition_previent_les_guichets(db_session, canaux):
    """
    La publication est dans `_transition`, le point unique par lequel passe tout
    changement d'état : aucune transition ne peut être oubliée. Ici CALLED →
    SERVING, cas d'un client qui se présente enfin.
    """
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    agent = await _agent_au_guichet(db_session, scene)

    ticket = await _ticket_appele(db_session, service, scene)
    canaux.publish.reset_mock()

    await service.start_serving(ticket.id, agent)

    evenements = canaux.events(EventType.COUNTER_QUEUE_CHANGED.value)
    assert len(evenements) == 1
    assert evenements[0]["reason"] == "serving"
    assert evenements[0]["status"] == "serving"
    assert evenements[0]["counter_id"] == scene["counter_a"].id
    assert evenements[0]["counter_name"] == "Guichet A1"


@pytest.mark.asyncio
async def test_un_evenement_guichet_non_publie_n_casse_pas_la_creation(db_session, canaux_en_panne):
    """
    Le temps réel est un accélérateur, pas une dépendance du chemin métier. Si
    Redis tombe, le ticket doit quand même être créé.
    """
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)

    created = await service.create_ticket(
        CreateTicketRequest(agency_id=scene["agency_a"].id, service_id=scene["service_a"].id),
        "client-1",
        scene["org_a"].id,
    )

    assert created.number == "A-001"
    ticket = (await db_session.execute(
        select(Ticket).where(Ticket.id == created.id)
    )).scalar_one()
    assert ticket.status == TicketStatus.WAITING
