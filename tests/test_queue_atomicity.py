"""
Tests d'atomicité du Queue Engine — contre un VRAI Redis.

Ces tests prouvent ce que le Redis mocké ne peut pas prouver :
  - un ticket n'est jamais servi deux fois (ZPOPMIN atomique) ;
  - les numéros de ticket sont uniques sous concurrence (INCR) ;
  - le verrou anti double création fonctionne (SET NX) ;
  - un ticket retiré de la file puis en échec est remis à sa place ;
  - un agent ne peut appeler que depuis son guichet ouvert.

Ils utilisent REDIS_URL (CI : service redis ; local : docker run -p 6391:6379 redis:7).
Si Redis est injoignable, les tests sont ignorés (skip), pas en échec.
"""
import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from fastapi import HTTPException
from sqlalchemy import select

import app.core.redis as redis_module
from app.core.config import settings
from app.core.redis import (
    acquire_ticket_creation_lock,
    add_to_queue,
    get_queue_size,
    next_ticket_sequence,
    pop_next_ticket,
    release_ticket_creation_lock,
)
from app.models.branch import Branch, Service
from app.models.counter import Counter
from app.models.employee import Employee
from app.models.organization import Organization
from app.models.ticket import Ticket, TicketStatus
from app.models.user import User, UserRole
from app.schemas.ticket import CreateTicketRequest
from tests.test_ticket_service import (
    _create_test_agency,
    _create_test_org,
    _create_test_service,
)


# ── Fixture : vrai Redis ──────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def real_redis():
    """
    Branche app.core.redis sur un vrai Redis (REDIS_URL) pour la durée du test.
    Supprime à la fin toutes les clés tonde:* créées par le test.
    """
    # Détection rapide (1 s) : un client jetable pour savoir si Redis répond
    probe = aioredis.from_url(settings.REDIS_URL, socket_connect_timeout=1)
    try:
        await asyncio.wait_for(probe.ping(), timeout=2)
    except Exception:
        pytest.skip(f"Redis injoignable sur {settings.REDIS_URL} — tests d'atomicité ignorés")
    finally:
        await probe.aclose()

    # Client de travail : délais par défaut (les tests de concurrence ouvrent 100 connexions à la fois)
    client = aioredis.from_url(settings.REDIS_URL, encoding="utf-8", decode_responses=True)

    keys_before = set(await client.keys("tonde:*"))
    previous = redis_module.redis_client
    redis_module.redis_client = client
    try:
        yield client
    finally:
        created = set(await client.keys("tonde:*")) - keys_before
        if created:
            await client.delete(*created)
        redis_module.redis_client = previous
        await client.aclose()


# ── Helpers de scénario ───────────────────────────────────────────────────────

async def _make_counter(db_session, org_id: str, agency_id: str, *, is_open: bool = True,
                        name: str = "Guichet 1") -> Counter:
    counter = Counter(org_id=org_id, agency_id=agency_id, name=name, is_open=is_open)
    db_session.add(counter)
    await db_session.commit()
    await db_session.refresh(counter)
    return counter


async def _make_staff(db_session, org_id: str, role: UserRole, *, counter_id: str | None = None,
                      agency_id: str | None = None) -> User:
    """Crée un utilisateur staff ; s'il a un guichet, crée aussi son Employee actif."""
    user = User(phone=f"+257{uuid.uuid4().int % 10**8:08d}", role=role, org_id=org_id)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    if counter_id:
        db_session.add(Employee(org_id=org_id, user_id=user.id, agency_id=agency_id,
                                counter_id=counter_id, role=role))
        await db_session.commit()
    return user


async def _world(db_session, *, counter_open: bool = True):
    """Org + branch ouverte + service + guichet + agent affecté à ce guichet."""
    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)
    counter = await _make_counter(db_session, org.id, agency.id, is_open=counter_open)
    agent = await _make_staff(db_session, org.id, UserRole.AGENT,
                              counter_id=counter.id, agency_id=agency.id)
    return org, agency, service, counter, agent


async def _new_ticket(ticket_service, agency, service, user_id: str):
    ticket = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency.id, service_id=service.id),
        user_id=user_id,
        org_id=None,
    )
    # Le score Redis contient l'horodatage en ms : on sépare les créations pour
    # que l'ordre FIFO attendu par les tests soit déterministe.
    await asyncio.sleep(0.01)
    return ticket


async def _call(ticket_service, org, agency, service, counter, actor):
    return await ticket_service.call_next(
        agency_id=agency.id, service_id=service.id, counter_id=counter.id,
        org_id=org.id, actor=actor,
    )


# ══ 1. Primitives Redis : atomicité ═══════════════════════════════════════════

@pytest.mark.asyncio
async def test_pop_respects_priority_then_fifo(real_redis):
    """ZPOPMIN sert l'urgence d'abord, puis FIFO dans la même priorité."""
    org, agency, service = (str(uuid.uuid4()) for _ in range(3))
    await add_to_queue(org, agency, service, "standard-1", priority=0)
    await asyncio.sleep(0.005)
    await add_to_queue(org, agency, service, "standard-2", priority=0)
    await add_to_queue(org, agency, service, "urgence", priority=9)

    order = []
    while (popped := await pop_next_ticket(org, agency, service)) is not None:
        order.append(popped[0])

    assert order == ["urgence", "standard-1", "standard-2"]


@pytest.mark.asyncio
async def test_concurrent_pop_never_returns_same_ticket(real_redis):
    """60 guichets simultanés sur 30 tickets : chaque ticket sort exactement une fois."""
    org, agency, service = (str(uuid.uuid4()) for _ in range(3))
    ids = {f"t-{i}" for i in range(30)}
    for tid in ids:
        await add_to_queue(org, agency, service, tid)

    results = await asyncio.gather(*(pop_next_ticket(org, agency, service) for _ in range(60)))

    popped = [r[0] for r in results if r is not None]
    assert len(popped) == 30, "tous les tickets doivent être servis"
    assert len(set(popped)) == 30, "aucun ticket ne doit être servi deux fois"
    assert set(popped) == ids
    assert sum(1 for r in results if r is None) == 30
    assert await get_queue_size(org, agency, service) == 0


@pytest.mark.asyncio
async def test_sequence_unique_under_concurrency(real_redis):
    """100 créations simultanées obtiennent 100 numéros distincts, de 1 à 100."""
    agency = str(uuid.uuid4())

    async def seed() -> int:
        return 0

    numbers = await asyncio.gather(
        *(next_ticket_sequence(agency, "A", "20260101", seed) for _ in range(100))
    )

    assert sorted(numbers) == list(range(1, 101))


@pytest.mark.asyncio
async def test_sequence_seeds_from_db_once(real_redis):
    """Compteur absent (Redis redémarré) : initialisé depuis la base, une seule fois."""
    agency = str(uuid.uuid4())
    calls = 0

    async def seed() -> int:
        nonlocal calls
        calls += 1
        return 41

    first = await next_ticket_sequence(agency, "B", "20260101", seed)
    second = await next_ticket_sequence(agency, "B", "20260101", seed)

    assert (first, second) == (42, 43)
    assert calls == 1


@pytest.mark.asyncio
async def test_creation_lock_blocks_until_released(real_redis):
    """Un second verrou pour le même utilisateur est refusé tant que le premier est tenu."""
    user = str(uuid.uuid4())

    assert await acquire_ticket_creation_lock(user) is True
    assert await acquire_ticket_creation_lock(user) is False
    assert await acquire_ticket_creation_lock(str(uuid.uuid4())) is True  # autre user : libre

    await release_ticket_creation_lock(user)
    assert await acquire_ticket_creation_lock(user) is True


# ══ 2. Numérotation : format, séquence, fuseau ════════════════════════════════

@pytest.mark.asyncio
async def test_ticket_numbers_are_padded_and_sequential(ticket_service, db_session, real_redis):
    """Les numéros sont A-001, A-002, A-003 (zéro-padding, séquence sans trou)."""
    org, agency, service, counter, agent = await _world(db_session)

    numbers = [
        (await _new_ticket(ticket_service, agency, service, f"client-{i}")).number
        for i in range(3)
    ]

    assert numbers == ["A-001", "A-002", "A-003"]


@pytest.mark.asyncio
async def test_sequence_resumes_from_db_after_redis_loss(ticket_service, db_session, real_redis):
    """Si Redis perd le compteur, la numérotation reprend après le dernier ticket en base."""
    org, agency, service, counter, agent = await _world(db_session)
    for i in range(2):
        await _new_ticket(ticket_service, agency, service, f"client-{i}")

    await real_redis.delete(*await real_redis.keys(f"tonde:seq:{agency.id}:*"))

    third = await _new_ticket(ticket_service, agency, service, "client-2")

    assert third.number == "A-003"


@pytest.mark.asyncio
async def test_sequence_day_uses_burundi_timezone(ticket_service, db_session, real_redis):
    """23h30 UTC = 01h30 le lendemain au Burundi : la clé du jour est celle du lendemain."""
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            frozen = datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc)
            return frozen.astimezone(tz) if tz else frozen

    agency_id = str(uuid.uuid4())
    with patch("app.services.ticket_service.datetime", FrozenDatetime):
        number = await ticket_service._get_next_sequence(agency_id, "A")

    assert number == 1
    assert await real_redis.exists(f"tonde:seq:{agency_id}:A:20260102") == 1
    assert await real_redis.exists(f"tonde:seq:{agency_id}:A:20260101") == 0


# ══ 3. Création : verrou anti double envoi ════════════════════════════════════

@pytest.mark.asyncio
async def test_create_ticket_rejected_while_creation_in_progress(ticket_service, db_session, real_redis):
    """Création pendant qu'une autre est en cours pour le même user → 409, aucun ticket créé."""
    org, agency, service, counter, agent = await _world(db_session)
    assert await acquire_ticket_creation_lock("client-double-tap") is True

    with pytest.raises(HTTPException) as exc_info:
        await _new_ticket(ticket_service, agency, service, "client-double-tap")

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "TICKET_CREATION_IN_PROGRESS"
    assert await get_queue_size(org.id, agency.id, service.id) == 0


@pytest.mark.asyncio
async def test_create_ticket_releases_lock_even_on_error(ticket_service, db_session, real_redis):
    """Une création en erreur (branch fermée) libère le verrou : l'utilisateur peut réessayer."""
    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id, is_open=False)
    service = await _create_test_service(db_session, agency.id, org.id)

    with pytest.raises(HTTPException):
        await _new_ticket(ticket_service, agency, service, "client-retry")

    assert await acquire_ticket_creation_lock("client-retry") is True


# ══ 4. call_next : atomicité + contrôle du guichet ════════════════════════════

@pytest.mark.asyncio
async def test_call_next_serves_each_ticket_once_in_order(ticket_service, db_session, real_redis):
    """Appels successifs : A-001, A-002, puis file vide. Le guichet affiche le ticket courant."""
    org, agency, service, counter, agent = await _world(db_session)
    t1 = await _new_ticket(ticket_service, agency, service, "client-1")
    t2 = await _new_ticket(ticket_service, agency, service, "client-2")

    first = await _call(ticket_service, org, agency, service, counter, agent)
    assert first["success"] is True
    assert first["ticket_id"] == t1.id
    assert first["ticket_number"] == "A-001"
    await db_session.refresh(counter)
    assert counter.current_ticket_id == t1.id

    second = await _call(ticket_service, org, agency, service, counter, agent)
    assert second["ticket_id"] == t2.id

    third = await _call(ticket_service, org, agency, service, counter, agent)
    assert third["success"] is False


@pytest.mark.asyncio
async def test_call_next_uses_counter_name_from_database(ticket_service, db_session, real_redis):
    """Le nom du guichet vient de la base : aucune valeur du client ne peut l'usurper."""
    org, agency, service, counter, agent = await _world(db_session)
    ticket = await _new_ticket(ticket_service, agency, service, "client-1")

    result = await _call(ticket_service, org, agency, service, counter, agent)

    assert result["counter_name"] == "Guichet 1"
    row = (await db_session.execute(select(Ticket).where(Ticket.id == ticket.id))).scalar_one()
    assert row.counter_name == "Guichet 1"
    assert row.counter_id == counter.id


@pytest.mark.asyncio
async def test_call_next_records_real_actor_in_audit_log(ticket_service, db_session, real_redis):
    """L'audit trail enregistre l'agent (user) qui a appelé, plus l'ID du guichet."""
    from app.models.queue_log import QueueLog, QueueLogAction

    org, agency, service, counter, agent = await _world(db_session)
    ticket = await _new_ticket(ticket_service, agency, service, "client-1")
    await _call(ticket_service, org, agency, service, counter, agent)

    log = (await db_session.execute(
        select(QueueLog).where(QueueLog.ticket_id == ticket.id,
                               QueueLog.action == QueueLogAction.TICKET_CALLED)
    )).scalar_one()
    assert log.actor_id == agent.id
    assert log.actor_role == "agent"
    assert log.counter_id == counter.id


@pytest.mark.asyncio
async def test_call_next_agent_not_assigned_is_refused_and_queue_untouched(
    ticket_service, db_session, real_redis
):
    """Un agent affecté à un autre guichet reçoit 403 ; le ticket reste en file."""
    org, agency, service, counter, agent = await _world(db_session)
    other_counter = await _make_counter(db_session, org.id, agency.id, name="Guichet 2")
    intruder = await _make_staff(db_session, org.id, UserRole.AGENT,
                                 counter_id=other_counter.id, agency_id=agency.id)
    await _new_ticket(ticket_service, agency, service, "client-1")

    with pytest.raises(HTTPException) as exc_info:
        await _call(ticket_service, org, agency, service, counter, intruder)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "COUNTER_NOT_ASSIGNED"
    assert await get_queue_size(org.id, agency.id, service.id) == 1


@pytest.mark.asyncio
async def test_call_next_agent_without_any_assignment_is_refused(ticket_service, db_session, real_redis):
    """Un agent sans aucune affectation de guichet ne peut appeler depuis aucun guichet."""
    org, agency, service, counter, agent = await _world(db_session)
    unassigned = await _make_staff(db_session, org.id, UserRole.AGENT)
    await _new_ticket(ticket_service, agency, service, "client-1")

    with pytest.raises(HTTPException) as exc_info:
        await _call(ticket_service, org, agency, service, counter, unassigned)

    assert exc_info.value.detail["code"] == "COUNTER_NOT_ASSIGNED"


@pytest.mark.asyncio
async def test_call_next_supervisor_can_use_any_counter(ticket_service, db_session, real_redis):
    """Un superviseur peut appeler depuis n'importe quel guichet de son organisation."""
    org, agency, service, counter, agent = await _world(db_session)
    supervisor = await _make_staff(db_session, org.id, UserRole.SUPERVISOR)
    ticket = await _new_ticket(ticket_service, agency, service, "client-1")

    result = await _call(ticket_service, org, agency, service, counter, supervisor)

    assert result["ticket_id"] == ticket.id


@pytest.mark.asyncio
async def test_call_next_closed_counter_is_refused(ticket_service, db_session, real_redis):
    """Guichet fermé → 400 COUNTER_CLOSED, ticket toujours en file."""
    org, agency, service, counter, agent = await _world(db_session, counter_open=False)
    await _new_ticket(ticket_service, agency, service, "client-1")

    with pytest.raises(HTTPException) as exc_info:
        await _call(ticket_service, org, agency, service, counter, agent)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "COUNTER_CLOSED"
    assert await get_queue_size(org.id, agency.id, service.id) == 1


@pytest.mark.asyncio
async def test_call_next_counter_of_other_branch_or_org_is_not_found(
    ticket_service, db_session, real_redis
):
    """Un guichet d'une autre branch ou d'une autre organisation est introuvable (404)."""
    org, agency, service, counter, agent = await _world(db_session)

    other_org = Organization(name="Autre Banque", slug="autre-banque", sector="bank")
    db_session.add(other_org)
    await db_session.commit()
    other_agency = await _create_test_agency(db_session, other_org.id)
    foreign_counter = await _make_counter(db_session, other_org.id, other_agency.id)

    # Guichet d'une autre organisation
    with pytest.raises(HTTPException) as exc_info:
        await _call(ticket_service, org, agency, service, foreign_counter, agent)
    assert exc_info.value.status_code == 404

    # Guichet de la même org mais d'une autre branch
    sibling_agency = Branch(org_id=org.id, name="Annexe", slug="annexe-xyz", sector="bank",
                            is_active=True, is_open=True)
    db_session.add(sibling_agency)
    await db_session.commit()
    sibling_counter = await _make_counter(db_session, org.id, sibling_agency.id)
    with pytest.raises(HTTPException) as exc_info:
        await _call(ticket_service, org, agency, service, sibling_counter, agent)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_call_next_service_of_other_branch_is_not_found(ticket_service, db_session, real_redis):
    """Un service qui n'appartient pas à la branch du guichet est refusé (404)."""
    org, agency, service, counter, agent = await _world(db_session)
    sibling_agency = Branch(org_id=org.id, name="Annexe", slug="annexe-svc", sector="bank",
                            is_active=True, is_open=True)
    db_session.add(sibling_agency)
    await db_session.commit()
    foreign_service = await _create_test_service(db_session, sibling_agency.id, org.id)

    with pytest.raises(HTTPException) as exc_info:
        await ticket_service.call_next(
            agency_id=agency.id, service_id=foreign_service.id, counter_id=counter.id,
            org_id=org.id, actor=agent,
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["code"] == "SERVICE_NOT_FOUND"


@pytest.mark.asyncio
async def test_call_next_skips_stale_queue_entries(ticket_service, db_session, real_redis):
    """Entrées périmées (ticket fantôme, ticket annulé resté en file) sont sautées."""
    org, agency, service, counter, agent = await _world(db_session)
    real = await _new_ticket(ticket_service, agency, service, "client-real")
    cancelled = await _new_ticket(ticket_service, agency, service, "client-cancelled")

    # Ticket annulé en base mais resté dans Redis (désynchronisation)
    row = (await db_session.execute(select(Ticket).where(Ticket.id == cancelled.id))).scalar_one()
    row.status = TicketStatus.CANCELLED
    await db_session.commit()

    # Entrée fantôme avec le meilleur score possible : sortirait en premier
    key = f"tonde:{org.id}:{agency.id}:{service.id}:queue"
    await real_redis.zadd(key, {"ghost-ticket": 0})

    # Le ticket annulé a été créé après le réel → le réel passe avant lui
    result = await _call(ticket_service, org, agency, service, counter, agent)

    assert result["ticket_id"] == real.id
    # Le fantôme est purgé ; le ticket annulé reste en file mais ne sera jamais appelé
    second = await _call(ticket_service, org, agency, service, counter, agent)
    assert second["success"] is False
    assert await get_queue_size(org.id, agency.id, service.id) == 0


@pytest.mark.asyncio
async def test_call_next_restores_ticket_to_original_place_on_failure(
    ticket_service, db_session, real_redis
):
    """Si le passage à CALLED échoue après le pop, le ticket retrouve sa place d'origine."""
    org, agency, service, counter, agent = await _world(db_session)
    first = await _new_ticket(ticket_service, agency, service, "client-1")
    await _new_ticket(ticket_service, agency, service, "client-2")

    # Identifiants lus AVANT l'échec : le rollback expire les objets ORM de la session.
    org_id, agency_id, service_id, first_id = org.id, agency.id, service.id, first.id
    key = f"tonde:{org_id}:{agency_id}:{service_id}:queue"
    original_score = await real_redis.zscore(key, first_id)

    with patch.object(ticket_service, "_log_action", new_callable=AsyncMock,
                      side_effect=RuntimeError("panne base de données")):
        with pytest.raises(RuntimeError):
            await _call(ticket_service, org, agency, service, counter, agent)

    assert await get_queue_size(org_id, agency_id, service_id) == 2, "aucun ticket perdu"
    assert await real_redis.zscore(key, first_id) == original_score, "même score = même place"
    assert (await real_redis.zrange(key, 0, 0))[0] == first_id, "toujours premier de la file"


@pytest.mark.asyncio
async def test_counter_freed_when_ticket_done_or_absent(ticket_service, db_session, real_redis):
    """Le guichet est libéré (current_ticket_id = NULL) quand son ticket est terminé ou absent."""
    org, agency, service, counter, agent = await _world(db_session)
    t1 = await _new_ticket(ticket_service, agency, service, "client-1")
    t2 = await _new_ticket(ticket_service, agency, service, "client-2")

    await _call(ticket_service, org, agency, service, counter, agent)
    await ticket_service.start_serving(t1.id, agent)
    await ticket_service.complete_ticket(t1.id, agent)
    await db_session.refresh(counter)
    assert counter.current_ticket_id is None

    await _call(ticket_service, org, agency, service, counter, agent)
    await db_session.refresh(counter)
    assert counter.current_ticket_id == t2.id
    await ticket_service.mark_absent(t2.id, agent)
    await db_session.refresh(counter)
    assert counter.current_ticket_id is None
