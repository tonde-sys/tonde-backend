"""
Tests des fonctions du Queue Engine — contre un VRAI Redis.

Couvre : transfert, confirmation de priorité, auto-absent (scheduler), expirations,
reconstruction de la file depuis PostgreSQL (auto-réparation), ETA par moyenne glissante,
idempotence, quotas / horaires, flux INCOMPLETE → requeue/close, périmètre des agents,
événements temps réel publiés par le service.

Utilisent REDIS_URL ; ignorés (skip) si Redis est injoignable.
"""
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.redis import get_queue_size
from app.models.audit_log import AuditAction, AuditLog
from app.models.branch import Branch, Service
from app.models.queue_log import QueueLog, QueueLogAction
from app.models.ticket import Ticket, TicketPriority, TicketStatus
from app.models.user import UserRole
from app.schemas.ticket import CreateTicketRequest
from app.services.queue_scheduler import QueueScheduler
from app.services.ticket_service import is_branch_open_now
from tests.test_queue_atomicity import (  # noqa: F401  (fixtures importées par nom)
    _call,
    _make_counter,
    _make_staff,
    _new_ticket,
    _world,
    real_redis,
)


async def _make_service(db_session, org_id: str, agency_id: str, prefix: str, name: str,
                        minutes: int = 5, active: bool = True) -> Service:
    service = Service(org_id=org_id, branch_id=agency_id, name=name, ticket_prefix=prefix,
                      avg_duration_minutes=minutes, is_active=active)
    db_session.add(service)
    await db_session.commit()
    await db_session.refresh(service)
    return service


async def _row(db_session, ticket_id: str) -> Ticket:
    return (await db_session.execute(
        select(Ticket).where(Ticket.id == ticket_id).execution_options(populate_existing=True)
    )).scalar_one()


def _scheduler(db_engine) -> QueueScheduler:
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    return QueueScheduler(session_factory=factory)


# ══ Transfert ═════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_transfer_moves_ticket_to_target_service_and_keeps_seniority(
    ticket_service, db_session, real_redis
):
    """CALLED → TRANSFERRED → WAITING dans le service cible, avec nouveau numéro et ancienneté."""
    org, agency, service, counter, agent = await _world(db_session)
    target = await _make_service(db_session, org.id, agency.id, "B", "Crédit")
    ticket = await _new_ticket(ticket_service, agency, service, "client-1")
    created_at = (await _row(db_session, ticket.id)).created_at
    org_id, agency_id, service_id, target_id = org.id, agency.id, service.id, target.id

    await _call(ticket_service, org, agency, service, counter, agent)
    result = await ticket_service.transfer_ticket(ticket.id, target_id, agent, reason="besoin d'un crédit")

    assert result["ticket_number"] == "B-001"
    assert result["position"] == 1
    row = await _row(db_session, ticket.id)
    assert row.status == TicketStatus.WAITING
    assert row.service_id == target_id and row.prefix == "B"
    assert row.counter_id is None and row.called_at is None
    assert row.created_at == created_at, "l'ancienneté doit être conservée"
    assert await get_queue_size(org_id, agency_id, service_id) == 0
    assert await get_queue_size(org_id, agency_id, target_id) == 1

    # Guichet libéré, échéances d'absence annulées
    await db_session.refresh(counter)
    assert counter.current_ticket_id is None
    assert await real_redis.zscore("tonde:deadlines:absent", f"{org_id}:{ticket.id}") is None


@pytest.mark.asyncio
async def test_transfer_is_audited_with_source_and_target(ticket_service, db_session, real_redis):
    """Le transfert laisse deux lignes QueueLog (sortie, entrée) et une ligne d'audit détaillée."""
    org, agency, service, counter, agent = await _world(db_session)
    target = await _make_service(db_session, org.id, agency.id, "B", "Crédit")
    ticket = await _new_ticket(ticket_service, agency, service, "client-1")
    service_id, target_id = service.id, target.id
    await _call(ticket_service, org, agency, service, counter, agent)

    await ticket_service.transfer_ticket(ticket.id, target_id, agent, reason="motif test")

    logs = {l.action: l for l in (await db_session.execute(
        select(QueueLog).where(QueueLog.ticket_id == ticket.id)
    )).scalars().all()}
    assert logs[QueueLogAction.TICKET_TRANSFERRED].service_id == service_id, "sortie tracée sur l'ancien service"
    assert logs[QueueLogAction.TICKET_RETURNED].service_id == target_id, "entrée tracée sur le service cible"
    audit = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == AuditAction.TICKET_TRANSFERRED)
    )).scalar_one()
    assert audit.actor_id == agent.id
    assert audit.extra_data["to_service_id"] == target_id and audit.extra_data["reason"] == "motif test"


@pytest.mark.asyncio
async def test_transfer_from_serving_is_allowed(ticket_service, db_session, real_redis):
    """Un ticket déjà SERVING peut aussi être transféré (le besoin apparaît pendant le service)."""
    org, agency, service, counter, agent = await _world(db_session)
    target = await _make_service(db_session, org.id, agency.id, "B", "Crédit")
    ticket = await _new_ticket(ticket_service, agency, service, "client-1")
    await _call(ticket_service, org, agency, service, counter, agent)
    await ticket_service.start_serving(ticket.id, agent)

    result = await ticket_service.transfer_ticket(ticket.id, target.id, agent)

    assert result["success"] is True
    assert (await _row(db_session, ticket.id)).served_at is None


@pytest.mark.asyncio
async def test_transfer_validation_errors(ticket_service, db_session, real_redis):
    """Même service → 400, service d'une autre branch → 404, service inactif → 400."""
    org, agency, service, counter, agent = await _world(db_session)
    inactive = await _make_service(db_session, org.id, agency.id, "C", "Fermé", active=False)
    other_branch = Branch(org_id=org.id, name="Annexe", slug="annexe-t", sector="bank",
                          is_active=True, is_open=True)
    db_session.add(other_branch)
    await db_session.commit()
    foreign = await _make_service(db_session, org.id, other_branch.id, "D", "Ailleurs")
    ticket = await _new_ticket(ticket_service, agency, service, "client-1")
    service_id, inactive_id, foreign_id = service.id, inactive.id, foreign.id
    await _call(ticket_service, org, agency, service, counter, agent)

    with pytest.raises(HTTPException) as exc:
        await ticket_service.transfer_ticket(ticket.id, service_id, agent)
    assert exc.value.detail["code"] == "SAME_SERVICE"

    with pytest.raises(HTTPException) as exc:
        await ticket_service.transfer_ticket(ticket.id, foreign_id, agent)
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        await ticket_service.transfer_ticket(ticket.id, inactive_id, agent)
    assert exc.value.detail["code"] == "SERVICE_INACTIVE"
    assert (await _row(db_session, ticket.id)).status == TicketStatus.CALLED, "aucun effet de bord"


@pytest.mark.asyncio
async def test_transfer_requires_called_or_serving_and_own_counter(ticket_service, db_session, real_redis):
    """Un ticket WAITING n'est pas transférable ; un agent n'agit que sur le ticket de SON guichet."""
    org, agency, service, counter, agent = await _world(db_session)
    target = await _make_service(db_session, org.id, agency.id, "B", "Crédit")
    supervisor = await _make_staff(db_session, org.id, UserRole.SUPERVISOR,
                                   counter_id=counter.id, agency_id=agency.id)
    other_counter = await _make_counter(db_session, org.id, agency.id, name="Guichet 2")
    intruder = await _make_staff(db_session, org.id, UserRole.AGENT,
                                 counter_id=other_counter.id, agency_id=agency.id)
    ticket = await _new_ticket(ticket_service, agency, service, "client-1")

    with pytest.raises(HTTPException) as exc:        # WAITING : transition interdite
        await ticket_service.transfer_ticket(ticket.id, target.id, supervisor)
    assert exc.value.detail["code"] == "INVALID_TRANSITION"

    await _call(ticket_service, org, agency, service, counter, agent)
    with pytest.raises(HTTPException) as exc:        # ticket du guichet 1, agent du guichet 2
        await ticket_service.transfer_ticket(ticket.id, target.id, intruder)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "TICKET_NOT_YOURS"


# ══ Priorité ══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_client_priority_request_is_pending_not_applied(ticket_service, db_session, real_redis):
    """Un client demande « vip » : le ticket reste standard tant qu'un agent n'a pas confirmé."""
    org, agency, service, counter, agent = await _world(db_session)

    created = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency.id, service_id=service.id, priority="vip"),
        user_id="client-vip", org_id=None,
    )

    assert created.priority == "standard"
    assert created.requested_priority == "vip"


@pytest.mark.asyncio
async def test_agent_approval_moves_ticket_ahead_without_losing_seniority(
    ticket_service, db_session, real_redis
):
    """Approuver une priorité fait passer le ticket devant les standards, ancienneté conservée."""
    org, agency, service, counter, agent = await _world(db_session)
    await _new_ticket(ticket_service, agency, service, "client-1")
    second = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency.id, service_id=service.id, priority="priority"),
        user_id="client-2", org_id=None,
    )
    assert second.position == 2

    result = await ticket_service.decide_priority(second.id, agent, approve=True)

    assert result["priority"] == "priority" and result["position"] == 1
    row = await _row(db_session, second.id)
    assert row.priority == TicketPriority.PRIORITY and row.requested_priority is None
    called = await _call(ticket_service, org, agency, service, counter, agent)
    assert called["ticket_id"] == second.id, "le ticket prioritaire est appelé en premier"


@pytest.mark.asyncio
async def test_agent_can_reject_priority_request(ticket_service, db_session, real_redis):
    """Rejeter une demande la supprime, le ticket garde sa place standard, l'audit le trace."""
    org, agency, service, counter, agent = await _world(db_session)
    await _new_ticket(ticket_service, agency, service, "client-1")
    second = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency.id, service_id=service.id, priority="emergency"),
        user_id="client-2", org_id=None,
    )

    result = await ticket_service.decide_priority(second.id, agent, approve=False)

    assert result["priority"] == "standard" and result["position"] == 2
    assert (await _row(db_session, second.id)).requested_priority is None
    audit = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == AuditAction.PRIORITY_REJECTED)
    )).scalar_one()
    assert audit.extra_data["requested"] == "emergency"


@pytest.mark.asyncio
async def test_agent_can_assign_priority_without_request(ticket_service, db_session, real_redis):
    """Un superviseur attribue directement une priorité (cas d'une personne âgée au guichet)."""
    org, agency, service, counter, agent = await _world(db_session)
    supervisor = await _make_staff(db_session, org.id, UserRole.SUPERVISOR,
                                   counter_id=counter.id, agency_id=agency.id)
    await _new_ticket(ticket_service, agency, service, "client-1")
    second = await _new_ticket(ticket_service, agency, service, "client-2")

    result = await ticket_service.decide_priority(second.id, supervisor, approve=True, priority="emergency")

    assert result["priority"] == "emergency" and result["position"] == 1
    audit = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == AuditAction.PRIORITY_SET)
    )).scalar_one()
    assert audit.actor_role == "supervisor"


@pytest.mark.asyncio
async def test_priority_decision_errors(ticket_service, db_session, real_redis):
    """Rien à rejeter/approuver → 400 ; ticket non WAITING → 400 ; autre branch → 403."""
    org, agency, service, counter, agent = await _world(db_session)
    other_branch = Branch(org_id=org.id, name="Annexe", slug="annexe-p", sector="bank",
                          is_active=True, is_open=True)
    db_session.add(other_branch)
    await db_session.commit()
    other_counter = await _make_counter(db_session, org.id, other_branch.id, name="Annexe 1")
    outsider = await _make_staff(db_session, org.id, UserRole.AGENT,
                                 counter_id=other_counter.id, agency_id=other_branch.id)
    ticket = await _new_ticket(ticket_service, agency, service, "client-1")

    with pytest.raises(HTTPException) as exc:        # aucune demande à rejeter
        await ticket_service.decide_priority(ticket.id, agent, approve=False)
    assert exc.value.detail["code"] == "NO_PRIORITY_REQUEST"
    with pytest.raises(HTTPException) as exc:        # aucune demande à approuver, rien de précisé
        await ticket_service.decide_priority(ticket.id, agent, approve=True)
    assert exc.value.detail["code"] == "NO_PRIORITY_REQUEST"
    with pytest.raises(HTTPException) as exc:        # agent d'une autre branch
        await ticket_service.decide_priority(ticket.id, outsider, approve=True, priority="vip")
    assert exc.value.detail["code"] == "BRANCH_FORBIDDEN"

    await _call(ticket_service, org, agency, service, counter, agent)
    with pytest.raises(HTTPException) as exc:        # déjà appelé : trop tard
        await ticket_service.decide_priority(ticket.id, agent, approve=True, priority="vip")
    assert exc.value.detail["code"] == "PRIORITY_NOT_CHANGEABLE"


# ══ Auto-absent et scheduler ══════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_call_schedules_warning_and_absent_deadlines(ticket_service, db_session, real_redis):
    """Appeler un ticket programme l'avertissement (120 s) et l'absence (180 s)."""
    org, agency, service, counter, agent = await _world(db_session)
    ticket = await _new_ticket(ticket_service, agency, service, "client-1")
    org_id = org.id
    before = time.time()

    await _call(ticket_service, org, agency, service, counter, agent)

    member = f"{org_id}:{ticket.id}"
    absent_at = await real_redis.zscore("tonde:deadlines:absent", member)
    warning_at = await real_redis.zscore("tonde:deadlines:warning", member)
    assert absent_at == pytest.approx(before + settings.ABSENT_TIMEOUT_SECONDS, abs=5)
    assert warning_at == pytest.approx(before + settings.ABSENT_WARNING_SECONDS, abs=5)


@pytest.mark.asyncio
async def test_scheduler_marks_called_ticket_absent_after_timeout(
    ticket_service, db_session, db_engine, real_redis
):
    """Passé le délai, le ticket appelé devient ABSENT automatiquement et le guichet est libéré."""
    org, agency, service, counter, agent = await _world(db_session)
    ticket = await _new_ticket(ticket_service, agency, service, "client-1")
    org_id = org.id
    await _call(ticket_service, org, agency, service, counter, agent)

    stats = await _scheduler(db_engine).run_once(now_ts=time.time() + settings.ABSENT_TIMEOUT_SECONDS + 1)

    assert stats["absent"] == 1 and stats["warnings"] == 1
    assert (await _row(db_session, ticket.id)).status == TicketStatus.ABSENT
    await db_session.refresh(counter)
    assert counter.current_ticket_id is None
    assert await real_redis.zscore("tonde:deadlines:absent", f"{org_id}:{ticket.id}") is None
    log = (await db_session.execute(
        select(QueueLog).where(QueueLog.ticket_id == ticket.id, QueueLog.action == QueueLogAction.TICKET_ABSENT)
    )).scalar_one()
    assert log.actor_role == "system"


@pytest.mark.asyncio
async def test_scheduler_does_not_mark_absent_before_timeout_or_after_presence(
    ticket_service, db_session, db_engine, real_redis
):
    """Avant l'échéance, ou si le client s'est présenté (SERVING), aucun passage en ABSENT."""
    org, agency, service, counter, agent = await _world(db_session)
    waiting = await _new_ticket(ticket_service, agency, service, "client-1")
    present = await _new_ticket(ticket_service, agency, service, "client-2")
    await _call(ticket_service, org, agency, service, counter, agent)   # appelle `waiting`
    await _call(ticket_service, org, agency, service, counter, agent)   # appelle `present`
    await ticket_service.start_serving(present.id, agent)               # client 2 s'est présenté

    early = await _scheduler(db_engine).run_once(now_ts=time.time() + 10)
    assert early["absent"] == 0

    late = await _scheduler(db_engine).run_once(now_ts=time.time() + settings.ABSENT_TIMEOUT_SECONDS + 5)
    assert late["absent"] == 1, "seul le client qui ne s'est pas présenté devient absent"
    assert (await _row(db_session, waiting.id)).status == TicketStatus.ABSENT
    assert (await _row(db_session, present.id)).status == TicketStatus.SERVING


@pytest.mark.asyncio
async def test_scheduler_is_idempotent_and_ignores_manual_absence(
    ticket_service, db_session, db_engine, real_redis
):
    """Une échéance n'est traitée qu'une fois ; un ticket déjà marqué absent par l'agent est ignoré."""
    org, agency, service, counter, agent = await _world(db_session)
    ticket = await _new_ticket(ticket_service, agency, service, "client-1")
    org_id = org.id
    await _call(ticket_service, org, agency, service, counter, agent)
    await ticket_service.mark_absent(ticket.id, agent)                  # l'agent l'a déjà fait
    future = time.time() + settings.ABSENT_TIMEOUT_SECONDS + 1

    assert await ticket_service.auto_mark_absent(org_id, ticket.id) is False
    scheduler = _scheduler(db_engine)
    first = await scheduler.run_once(now_ts=future)
    second = await scheduler.run_once(now_ts=future)

    assert first["absent"] == 0 and second["absent"] == 0
    assert (await _row(db_session, ticket.id)).status == TicketStatus.ABSENT


@pytest.mark.asyncio
async def test_scheduler_isolated_failure_does_not_stop_others(
    ticket_service, db_session, db_engine, real_redis
):
    """Une échéance sur un ticket inexistant est ignorée sans empêcher le traitement des autres."""
    org, agency, service, counter, agent = await _world(db_session)
    ticket = await _new_ticket(ticket_service, agency, service, "client-1")
    org_id = org.id
    await _call(ticket_service, org, agency, service, counter, agent)
    await real_redis.zadd("tonde:deadlines:absent", {f"{org_id}:ghost-ticket": 1})   # échéance orpheline

    stats = await _scheduler(db_engine).run_once(now_ts=time.time() + settings.ABSENT_TIMEOUT_SECONDS + 1)

    assert stats["absent"] == 1
    assert (await _row(db_session, ticket.id)).status == TicketStatus.ABSENT


@pytest.mark.asyncio
async def test_warm_up_restores_queue_and_deadlines_after_redis_loss(
    ticket_service, db_session, db_engine, real_redis
):
    """Redis vidé : le warm-up remet les tickets WAITING en file et reprogramme les échéances."""
    org, agency, service, counter, agent = await _world(db_session)
    first = await _new_ticket(ticket_service, agency, service, "client-1")
    await _new_ticket(ticket_service, agency, service, "client-2")
    org_id, agency_id, service_id = org.id, agency.id, service.id
    await _call(ticket_service, org, agency, service, counter, agent)   # first CALLED, second WAITING

    for key in await real_redis.keys(f"tonde:*{org_id}*"):
        await real_redis.delete(key)                                    # perte totale de Redis
    summary = await _scheduler(db_engine).warm_up()

    assert summary == {"queued": 1, "deadlines": 1}
    assert await get_queue_size(org_id, agency_id, service_id) == 1
    assert await real_redis.zscore("tonde:deadlines:absent", f"{org_id}:{first.id}") is not None


# ══ Expirations ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_expire_stale_tickets_frees_blocked_clients(ticket_service, db_session, real_redis):
    """Chaque statut actif bloqué est clos, et le client peut reprendre un ticket."""
    org, agency, service, counter, agent = await _world(db_session)
    now = datetime.now(timezone.utc)
    tickets = {}
    for name in ("waiting", "absent", "serving", "incomplete", "fresh"):
        tickets[name] = await _new_ticket(ticket_service, agency, service, f"client-{name}")
    org_id, agency_id, service_id = org.id, agency.id, service.id

    async def patch_ticket(name, **fields):
        row = await _row(db_session, tickets[name].id)
        for key, value in fields.items():
            setattr(row, key, value)
        await db_session.commit()

    await patch_ticket("waiting", created_at=now - timedelta(hours=settings.WAITING_TICKET_TTL_HOURS + 1))
    await patch_ticket("absent", status=TicketStatus.ABSENT,
                       called_at=now - timedelta(minutes=settings.ABSENT_RETURN_WINDOW_MINUTES + 1))
    await patch_ticket("serving", status=TicketStatus.SERVING,
                       served_at=now - timedelta(hours=settings.SERVING_TICKET_TTL_HOURS + 1))
    await patch_ticket("incomplete", status=TicketStatus.INCOMPLETE,
                       served_at=now - timedelta(hours=settings.INCOMPLETE_TICKET_TTL_HOURS + 1))

    counts = await ticket_service.expire_stale_tickets()

    assert counts == {"waiting": 1, "absent": 1, "serving": 1, "incomplete": 1}
    assert (await _row(db_session, tickets["waiting"].id)).status == TicketStatus.CANCELLED
    assert (await _row(db_session, tickets["absent"].id)).status == TicketStatus.CANCELLED
    assert (await _row(db_session, tickets["serving"].id)).status == TicketStatus.INCOMPLETE
    assert (await _row(db_session, tickets["incomplete"].id)).status == TicketStatus.CANCELLED
    assert (await _row(db_session, tickets["fresh"].id)).status == TicketStatus.WAITING, "ticket récent intact"
    # Tous les tickets clos sont retirés de la file ; seul le ticket récent y reste
    key = f"tonde:{org_id}:{agency_id}:{service_id}:queue"
    assert await real_redis.zrange(key, 0, -1) == [tickets["fresh"].id]

    # Le client dont le ticket WAITING a expiré peut de nouveau prendre un ticket
    again = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency_id, service_id=service_id), "client-waiting", None
    )
    assert again.status == "waiting"


# ══ Reconstruction et auto-réparation de la file ══════════════════════════════

@pytest.mark.asyncio
async def test_rebuild_queue_restores_order_priority_and_seniority(ticket_service, db_session, real_redis):
    """La reconstruction depuis PostgreSQL respecte priorité puis ancienneté."""
    org, agency, service, counter, agent = await _world(db_session)
    first = await _new_ticket(ticket_service, agency, service, "client-1")
    second = await _new_ticket(ticket_service, agency, service, "client-2")
    vip = await _new_ticket(ticket_service, agency, service, "client-3")
    await ticket_service.decide_priority(vip.id, agent, approve=True, priority="vip")
    org_id, agency_id, service_id = org.id, agency.id, service.id
    key = f"tonde:{org_id}:{agency_id}:{service_id}:queue"
    await real_redis.delete(key)

    restored = await ticket_service.rebuild_queue()

    assert restored == 3
    assert await real_redis.zrange(key, 0, -1) == [vip.id, first.id, second.id]


@pytest.mark.asyncio
async def test_rebuild_never_overwrites_existing_scores(ticket_service, db_session, real_redis):
    """ZADD NX : une file saine n'est pas modifiée par une reconstruction."""
    org, agency, service, counter, agent = await _world(db_session)
    ticket = await _new_ticket(ticket_service, agency, service, "client-1")
    key = f"tonde:{org.id}:{agency.id}:{service.id}:queue"
    original = await real_redis.zscore(key, ticket.id)

    assert await ticket_service.rebuild_queue() == 0
    assert await real_redis.zscore(key, ticket.id) == original


@pytest.mark.asyncio
async def test_get_ticket_self_heals_when_missing_from_redis(ticket_service, db_session, real_redis):
    """Un ticket WAITING absent de Redis est remis en file quand le client consulte son état."""
    org, agency, service, counter, agent = await _world(db_session)
    ticket = await _new_ticket(ticket_service, agency, service, "client-1")
    key = f"tonde:{org.id}:{agency.id}:{service.id}:queue"
    await real_redis.delete(key)

    view = await ticket_service.get_ticket(ticket.id, "client-1", None)

    assert view.position == 1 and view.total_in_queue == 1
    assert await real_redis.zscore(key, ticket.id) is not None


@pytest.mark.asyncio
async def test_call_next_rebuilds_queue_when_redis_is_empty(ticket_service, db_session, real_redis):
    """File Redis perdue : l'appel reconstruit depuis la base et sert quand même le ticket."""
    org, agency, service, counter, agent = await _world(db_session)
    ticket = await _new_ticket(ticket_service, agency, service, "client-1")
    await real_redis.delete(f"tonde:{org.id}:{agency.id}:{service.id}:queue")

    result = await _call(ticket_service, org, agency, service, counter, agent)

    assert result["success"] is True and result["ticket_id"] == ticket.id


@pytest.mark.asyncio
async def test_create_ticket_cancelled_cleanly_when_redis_fails(ticket_service, db_session, real_redis):
    """Redis en panne à la création : 503, ticket annulé (pas d'orphelin), le client peut réessayer."""
    org, agency, service, counter, agent = await _world(db_session)
    agency_id, service_id = agency.id, service.id

    with patch("app.services.ticket_service.add_to_queue", new_callable=AsyncMock,
               side_effect=ConnectionError("redis down")):
        with pytest.raises(HTTPException) as exc:
            await ticket_service.create_ticket(
                CreateTicketRequest(agency_id=agency_id, service_id=service_id), "client-1", None
            )
    assert exc.value.status_code == 503 and exc.value.detail["code"] == "QUEUE_UNAVAILABLE"
    row = (await db_session.execute(select(Ticket).where(Ticket.user_id == "client-1"))).scalar_one()
    assert row.status == TicketStatus.CANCELLED

    retry = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency_id, service_id=service_id), "client-1", None
    )
    assert retry.status == "waiting"


# ══ ETA ═══════════════════════════════════════════════════════════════════════

async def _add_done_tickets(db_session, org, agency, service, durations: list[int]) -> None:
    """Insère des tickets DONE avec des durées de service réelles."""
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    for index, seconds in enumerate(durations):
        db_session.add(Ticket(
            number=f"A-{900 + index}", prefix="A", sequence=900 + index, user_id=f"hist-{index}",
            org_id=org.id, agency_id=agency.id, service_id=service.id,
            status=TicketStatus.DONE, service_seconds=seconds,
            done_at=base + timedelta(minutes=index),
        ))
    await db_session.commit()


@pytest.mark.asyncio
async def test_eta_uses_configured_duration_without_history(ticket_service, db_session, real_redis):
    """Sans historique : durée configurée du service (5 min) × personnes devant."""
    org, agency, service, counter, agent = await _world(db_session)

    assert await ticket_service.estimate_wait_minutes(service, 1) == 0, "prochain : pas d'attente"
    assert await ticket_service.estimate_wait_minutes(service, 3) == 10   # 2 devant × 5 min


@pytest.mark.asyncio
async def test_eta_follows_rolling_average_of_real_services(ticket_service, db_session, real_redis):
    """Dix services réels de 60 s : l'ETA suit la réalité (2 min pour 2 personnes devant)."""
    org, agency, service, counter, agent = await _world(db_session)
    await _add_done_tickets(db_session, org, agency, service, [60] * 10)

    assert await ticket_service.estimate_wait_minutes(service, 3) == 2


@pytest.mark.asyncio
async def test_eta_is_smoothed_while_history_is_short(ticket_service, db_session, real_redis):
    """Peu d'historique : la durée configurée complète la moyenne (pas d'emballement)."""
    org, agency, service, counter, agent = await _world(db_session)
    await _add_done_tickets(db_session, org, agency, service, [120, 120])

    # (120+120 + 8×300) / 10 = 264 s ; 1 personne devant → 264 s → 5 min (arrondi supérieur)
    assert await ticket_service.estimate_wait_minutes(service, 2) == 5


@pytest.mark.asyncio
async def test_eta_ignores_other_services(ticket_service, db_session, real_redis):
    """L'historique d'un autre service n'influence pas l'ETA de celui-ci."""
    org, agency, service, counter, agent = await _world(db_session)
    other = await _make_service(db_session, org.id, agency.id, "B", "Crédit")
    await _add_done_tickets(db_session, org, agency, other, [10] * 10)

    assert await ticket_service.estimate_wait_minutes(service, 3) == 10


@pytest.mark.asyncio
async def test_real_wait_and_service_duration_are_recorded(ticket_service, db_session, real_redis):
    """À l'appel : attente réelle ; à la fin : durée réelle du service (secondes)."""
    org, agency, service, counter, agent = await _world(db_session)
    ticket = await _new_ticket(ticket_service, agency, service, "client-1")
    row = await _row(db_session, ticket.id)
    row.created_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    await db_session.commit()

    await _call(ticket_service, org, agency, service, counter, agent)
    await ticket_service.start_serving(ticket.id, agent)
    row = await _row(db_session, ticket.id)
    row.served_at = datetime.now(timezone.utc) - timedelta(seconds=90)
    await db_session.commit()
    await ticket_service.complete_ticket(ticket.id, agent)

    row = await _row(db_session, ticket.id)
    assert row.actual_wait_minutes == 10
    assert row.service_seconds in (90, 91)


# ══ Idempotence, quotas, horaires ═════════════════════════════════════════════

@pytest.mark.asyncio
async def test_same_client_action_id_creates_one_ticket(ticket_service, db_session, real_redis):
    """Rejouer la même création (réseau instable) renvoie le même ticket, sans doublon."""
    org, agency, service, counter, agent = await _world(db_session)
    request = CreateTicketRequest(agency_id=agency.id, service_id=service.id,
                                  client_action_id="offline-action-0001")

    first = await ticket_service.create_ticket(request, "client-1", None)
    replay = await ticket_service.create_ticket(request, "client-1", None)

    assert replay.id == first.id and replay.number == first.number
    count = len((await db_session.execute(select(Ticket).where(Ticket.user_id == "client-1"))).scalars().all())
    assert count == 1


@pytest.mark.asyncio
async def test_client_action_id_is_scoped_per_user(ticket_service, db_session, real_redis):
    """Le même identifiant d'action chez deux utilisateurs différents ne se confond pas."""
    org, agency, service, counter, agent = await _world(db_session)
    request = CreateTicketRequest(agency_id=agency.id, service_id=service.id,
                                  client_action_id="offline-action-0002")

    a = await ticket_service.create_ticket(request, "client-a", None)
    b = await ticket_service.create_ticket(request, "client-b", None)

    assert a.id != b.id


def test_client_action_id_format_is_validated():
    """Un identifiant d'action trop court ou avec des caractères exotiques est refusé (422)."""
    for bad in ("x", "has space 12345", "é" * 10, "a" * 65):
        with pytest.raises(ValidationError):
            CreateTicketRequest(agency_id="a", service_id="s", client_action_id=bad)
    assert CreateTicketRequest(agency_id="a", service_id="s",
                               client_action_id=str(uuid.uuid4())).client_action_id


@pytest.mark.asyncio
async def test_daily_ticket_limit_is_enforced(ticket_service, db_session, real_redis):
    """Branch.max_daily_tickets plafonne les créations de la journée (409 DAILY_LIMIT_REACHED)."""
    org, agency, service, counter, agent = await _world(db_session)
    branch = (await db_session.execute(select(Branch).where(Branch.id == agency.id))).scalar_one()
    branch.max_daily_tickets = 1
    await db_session.commit()
    await _new_ticket(ticket_service, agency, service, "client-1")

    with pytest.raises(HTTPException) as exc:
        await _new_ticket(ticket_service, agency, service, "client-2")

    assert exc.value.status_code == 409 and exc.value.detail["code"] == "DAILY_LIMIT_REACHED"


@pytest.mark.asyncio
async def test_daily_limit_zero_means_unlimited(ticket_service, db_session, real_redis):
    """max_daily_tickets = 0 signifie « illimité » : il ne doit surtout pas tout refuser."""
    org, agency, service, counter, agent = await _world(db_session)
    branch = (await db_session.execute(select(Branch).where(Branch.id == agency.id))).scalar_one()
    branch.max_daily_tickets = 0
    await db_session.commit()

    for index in range(3):
        created = await _new_ticket(ticket_service, agency, service, f"client-{index}")
        assert created.status == "waiting"


@pytest.mark.asyncio
async def test_inactive_service_refuses_new_tickets(ticket_service, db_session, real_redis):
    """Un service désactivé n'accepte plus de ticket (400 SERVICE_INACTIVE)."""
    org, agency, service, counter, agent = await _world(db_session)
    closed = await _make_service(db_session, org.id, agency.id, "Z", "Fermé", active=False)

    with pytest.raises(HTTPException) as exc:
        await ticket_service.create_ticket(
            CreateTicketRequest(agency_id=agency.id, service_id=closed.id), "client-1", None
        )

    assert exc.value.detail["code"] == "SERVICE_INACTIVE"


def test_branch_open_now_follows_operating_hours():
    """Horaires détaillés : ouvert dans le créneau du jour, fermé hors créneau ou jour absent."""
    branch = Branch(
        name="B", slug="b", sector="bank", is_active=True, is_open=True,
        operating_hours={"monday": {"open": "08:00", "close": "17:00"}},
    )
    monday_10h = datetime(2026, 11, 9, 10, 0)    # un lundi
    monday_18h = datetime(2026, 11, 9, 18, 0)
    tuesday_10h = datetime(2026, 11, 10, 10, 0)  # mardi : absent des horaires

    assert is_branch_open_now(branch, monday_10h) is True
    assert is_branch_open_now(branch, monday_18h) is False
    assert is_branch_open_now(branch, tuesday_10h) is False

    branch.operating_hours = None                 # sans horaires : seul l'interrupteur compte
    assert is_branch_open_now(branch, tuesday_10h) is True
    branch.is_open = False
    assert is_branch_open_now(branch, monday_10h) is False
    branch.is_open, branch.is_active = True, False
    assert is_branch_open_now(branch, monday_10h) is False


# ══ INCOMPLETE : requeue / close ══════════════════════════════════════════════

@pytest.mark.asyncio
async def test_incomplete_keeps_client_blocked_until_agent_resolves(ticket_service, db_session, real_redis):
    """INCOMPLETE reste actif (409 pour un nouveau ticket) jusqu'à requeue ou close."""
    org, agency, service, counter, agent = await _world(db_session)
    ticket = await _new_ticket(ticket_service, agency, service, "client-1")
    agency_id, service_id = agency.id, service.id
    await _call(ticket_service, org, agency, service, counter, agent)
    await ticket_service.start_serving(ticket.id, agent)

    await ticket_service.mark_incomplete(ticket.id, agent, reason="système en panne")

    assert (await _row(db_session, ticket.id)).status == TicketStatus.INCOMPLETE
    await db_session.refresh(counter)
    assert counter.current_ticket_id is None
    with pytest.raises(HTTPException) as exc:
        await ticket_service.create_ticket(
            CreateTicketRequest(agency_id=agency_id, service_id=service_id), "client-1", None
        )
    assert exc.value.detail["code"] == "TICKET_ALREADY_ACTIVE"


@pytest.mark.asyncio
async def test_requeue_incomplete_puts_ticket_back_with_seniority(ticket_service, db_session, real_redis):
    """requeue : INCOMPLETE → WAITING, devant ceux arrivés après (ancienneté conservée)."""
    org, agency, service, counter, agent = await _world(db_session)
    first = await _new_ticket(ticket_service, agency, service, "client-1")
    await _call(ticket_service, org, agency, service, counter, agent)
    await ticket_service.start_serving(first.id, agent)
    later = await _new_ticket(ticket_service, agency, service, "client-2")
    await ticket_service.mark_incomplete(first.id, agent)

    result = await ticket_service.requeue_ticket(first.id, agent)

    assert result["position"] == 1, "arrivé avant client-2, il repasse devant"
    called = await _call(ticket_service, org, agency, service, counter, agent)
    assert called["ticket_id"] == first.id and later.id != first.id


@pytest.mark.asyncio
async def test_close_ticket_frees_the_client(ticket_service, db_session, real_redis):
    """close : un agent clôt un ticket ABSENT/WAITING/INCOMPLETE ; le client peut reprendre un ticket."""
    org, agency, service, counter, agent = await _world(db_session)
    ticket = await _new_ticket(ticket_service, agency, service, "client-1")
    agency_id, service_id, org_id = agency.id, service.id, org.id
    await _call(ticket_service, org, agency, service, counter, agent)
    await ticket_service.mark_absent(ticket.id, agent)

    await ticket_service.close_ticket(ticket.id, agent, reason="n'est jamais revenu")

    assert (await _row(db_session, ticket.id)).status == TicketStatus.CANCELLED
    again = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency_id, service_id=service_id), "client-1", None
    )
    assert again.status == "waiting"
    assert await get_queue_size(org_id, agency_id, service_id) == 1


@pytest.mark.asyncio
async def test_staff_return_to_queue_is_scoped_to_branch(ticket_service, db_session, real_redis):
    """Un agent remet en file un ticket ABSENT de sa branch ; pas celui d'une autre branch."""
    org, agency, service, counter, agent = await _world(db_session)
    other_branch = Branch(org_id=org.id, name="Annexe", slug="annexe-r", sector="bank",
                          is_active=True, is_open=True)
    db_session.add(other_branch)
    await db_session.commit()
    other_counter = await _make_counter(db_session, org.id, other_branch.id, name="Annexe 1")
    outsider = await _make_staff(db_session, org.id, UserRole.AGENT,
                                 counter_id=other_counter.id, agency_id=other_branch.id)
    ticket = await _new_ticket(ticket_service, agency, service, "client-1")
    org_id = org.id
    await _call(ticket_service, org, agency, service, counter, agent)
    await ticket_service.mark_absent(ticket.id, agent)

    with pytest.raises(HTTPException) as exc:
        await ticket_service.return_to_queue(ticket.id, org_id, actor=outsider)
    assert exc.value.status_code == 403

    result = await ticket_service.return_to_queue(ticket.id, org_id, actor=agent)
    assert result["success"] is True
    assert (await _row(db_session, ticket.id)).status == TicketStatus.WAITING


# ══ Périmètre des agents ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_agent_cannot_serve_or_finish_another_counters_ticket(ticket_service, db_session, real_redis):
    """serving / done / absent / incomplete sont limités au guichet de l'agent."""
    org, agency, service, counter, agent = await _world(db_session)
    other_counter = await _make_counter(db_session, org.id, agency.id, name="Guichet 2")
    intruder = await _make_staff(db_session, org.id, UserRole.AGENT,
                                 counter_id=other_counter.id, agency_id=agency.id)
    ticket = await _new_ticket(ticket_service, agency, service, "client-1")
    await _call(ticket_service, org, agency, service, counter, agent)

    for action in (
        ticket_service.start_serving(ticket.id, intruder),
        ticket_service.mark_absent(ticket.id, intruder),
        ticket_service.complete_ticket(ticket.id, intruder),
        ticket_service.mark_incomplete(ticket.id, intruder),
    ):
        with pytest.raises(HTTPException) as exc:
            await action
        assert exc.value.status_code == 403 and exc.value.detail["code"] == "TICKET_NOT_YOURS"
    assert (await _row(db_session, ticket.id)).status == TicketStatus.CALLED


@pytest.mark.asyncio
async def test_staff_of_another_organization_cannot_reach_ticket(ticket_service, db_session, real_redis):
    """Un agent d'une autre organisation voit « ticket introuvable » (isolation multi-tenant)."""
    from app.models.organization import Organization
    org, agency, service, counter, agent = await _world(db_session)
    ticket = await _new_ticket(ticket_service, agency, service, "client-1")
    await _call(ticket_service, org, agency, service, counter, agent)
    other_org = Organization(name="Autre", slug="autre-org", sector="bank")
    db_session.add(other_org)
    await db_session.commit()
    stranger = await _make_staff(db_session, other_org.id, UserRole.SUPERVISOR)

    with pytest.raises(HTTPException) as exc:
        await ticket_service.start_serving(ticket.id, stranger)

    assert exc.value.status_code == 404


# ══ Événements temps réel émis par le service ═════════════════════════════════

@pytest.mark.asyncio
async def test_call_next_publishes_realtime_events_from_service(ticket_service, db_session, real_redis):
    """call_next publie your_turn (personnel), ticket_called (salle) et les nouvelles positions."""
    org, agency, service, counter, agent = await _world(db_session)
    first = await _new_ticket(ticket_service, agency, service, "client-1")
    second = await _new_ticket(ticket_service, agency, service, "client-2")
    agency_id = agency.id

    with patch("app.services.ticket_service.publish_event", new_callable=AsyncMock) as publish:
        await _call(ticket_service, org, agency, service, counter, agent)

    events = {call.args[1]["type"]: call.args[1] for call in publish.call_args_list}
    assert events["your_turn"]["ticket_id"] == first.id
    assert events["your_turn"]["counter_name"] == "Guichet 1"
    assert events["your_turn"]["timeout_seconds"] == settings.ABSENT_TIMEOUT_SECONDS
    assert events["ticket_called"]["called_number"] == "A-001"
    assert events["ticket_called"]["agency_id"] == agency_id
    snapshot = events["queue_snapshot"]
    assert snapshot["positions"] == {second.id: 1}, "client-2 devient le prochain"


@pytest.mark.asyncio
async def test_done_and_absent_publish_personal_events(ticket_service, db_session, real_redis):
    """Terminer ou déclarer absent prévient le client concerné."""
    org, agency, service, counter, agent = await _world(db_session)
    done = await _new_ticket(ticket_service, agency, service, "client-1")
    absent = await _new_ticket(ticket_service, agency, service, "client-2")
    await _call(ticket_service, org, agency, service, counter, agent)
    await ticket_service.start_serving(done.id, agent)

    with patch("app.services.ticket_service.publish_event", new_callable=AsyncMock) as publish:
        await ticket_service.complete_ticket(done.id, agent)
        await _call(ticket_service, org, agency, service, counter, agent)
        await ticket_service.mark_absent(absent.id, agent)

    types = [(c.args[1]["type"], c.args[1].get("ticket_id")) for c in publish.call_args_list]
    assert ("ticket_done", done.id) in types
    assert ("ticket_absent", absent.id) in types


@pytest.mark.asyncio
async def test_realtime_failure_never_breaks_the_business_operation(ticket_service, db_session, real_redis):
    """Redis Pub/Sub en panne : le ticket est quand même appelé."""
    org, agency, service, counter, agent = await _world(db_session)
    ticket = await _new_ticket(ticket_service, agency, service, "client-1")

    with patch("app.websocket.queue_ws.ws_manager.publish_event", new_callable=AsyncMock,
               side_effect=ConnectionError("pubsub down")):
        result = await _call(ticket_service, org, agency, service, counter, agent)

    assert result["success"] is True
    assert (await _row(db_session, ticket.id)).status == TicketStatus.CALLED
