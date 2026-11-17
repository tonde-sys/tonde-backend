"""Historical origin evidence, dry-run, idempotence and resume."""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.branch import Service
from app.models.qape import AbuseEvent
from app.models.queue_log import QueueLog, QueueLogAction
from app.models.ticket import Ticket, TicketStatus
from app.models.user import User
from app.services.qape_backfill import run_backfill
from app.services.qape_data import EventRepository
from tests.test_qape_data import world


async def ticket_with_history(db, org, branch, service, user, status, transfer=None, created_log=True, ticket_id=None):
    created_at = datetime.now(timezone.utc) - timedelta(days=1)
    ticket = Ticket(**({"id": ticket_id} if ticket_id else {}), org_id=org.id, agency_id=branch.id, service_id=service.id,
                    user_id=user.id, number="A-001", sequence=1,
                    queue_sequence=1, status=status, created_at=created_at)
    db.add(ticket)
    await db.flush()
    if created_log:
        db.add(QueueLog(org_id=org.id, agency_id=branch.id, service_id=service.id,
                        ticket_id=ticket.id, action=QueueLogAction.TICKET_CREATED,
                        to_status="waiting", created_at=created_at+timedelta(seconds=1)))
    if transfer is not None:
        db.add(QueueLog(org_id=org.id, agency_id=branch.id, service_id=service.id,
                        ticket_id=ticket.id, action=QueueLogAction.TICKET_TRANSFERRED,
                        from_status="called", to_status="transferred",
                        extra_data=json.dumps({"from_service_id": service.id,
                                               "to_service_id": transfer.id}),
                        created_at=created_at+timedelta(minutes=5)))
        ticket.service_id = transfer.id
    await db.commit()
    return ticket


@pytest.mark.asyncio
async def test_dry_run_real_run_and_repeat(db_session):
    db = db_session
    org, _, service, user, branch = await world(db)
    ticket = await ticket_with_history(db, org, branch, service, user, TicketStatus.CANCELLED)
    org_id, service_id, ticket_created_at = org.id, service.id, ticket.created_at
    before = await EventRepository(db).count_created(org_id, service_id,
        datetime.now(timezone.utc)-timedelta(days=2), datetime.now(timezone.utc))
    assert before == 0
    preview = await run_backfill(db, batch_size=1)
    assert preview.recoverable == 1 and preview.inserted == 0 and preview.dry_run
    assert (await db.scalars(select(AbuseEvent))).all() == []
    report = await run_backfill(db, dry_run=False, batch_size=1)
    assert report.inserted == 1 and report.ambiguous == []
    event = (await db.scalars(select(AbuseEvent))).one()
    assert event.decision is None and event.provenance == "HISTORICAL_CERTAIN"
    assert event.created_at.replace(tzinfo=timezone.utc) == ticket_created_at
    assert event.service_id == service_id
    assert (await EventRepository(db).count_created(org_id, service_id,
        datetime.now(timezone.utc)-timedelta(days=2), datetime.now(timezone.utc))) == 1
    again = await run_backfill(db, dry_run=False)
    assert again.inserted == 0 and again.already_recorded == 1


@pytest.mark.asyncio
async def test_transfer_counts_once_at_original_service(db_session):
    db = db_session
    org, _, source, user, branch = await world(db)
    target = Service(org_id=org.id, branch_id=branch.id, name="Target")
    db.add(target); await db.commit()
    ticket = await ticket_with_history(db, org, branch, source, user, TicketStatus.DONE, transfer=target)
    report = await run_backfill(db, dry_run=False)
    assert report.inserted == 1
    event = (await db.scalars(select(AbuseEvent))).one()
    assert ticket.service_id == target.id and event.service_id == source.id
    start, end = datetime.now(timezone.utc)-timedelta(days=2), datetime.now(timezone.utc)
    repo = EventRepository(db)
    assert await repo.count_created(org.id, source.id, start, end) == 1
    assert await repo.count_created(org.id, target.id, start, end) == 0


@pytest.mark.asyncio
async def test_missing_transfer_and_missing_origin_are_ambiguous(db_session):
    db = db_session
    org, _, source, user, branch = await world(db)
    target = Service(org_id=org.id, branch_id=branch.id, name="Target")
    db.add(target); await db.commit()
    incomplete = await ticket_with_history(db, org, branch, source, user, TicketStatus.ABSENT,
                                           transfer=target, created_log=False)
    other_user = User(phone="+257" + str(uuid.uuid4().int % 10**8).zfill(8))
    db.add(other_user); await db.commit()
    unknown = await ticket_with_history(db, org, branch, source, other_user, TicketStatus.DONE,
                                        created_log=False)
    report = await run_backfill(db, dry_run=False)
    assert report.inserted == 0
    assert {x["ticket_id"] for x in report.ambiguous} == {incomplete.id, unknown.id}


@pytest.mark.asyncio
async def test_states_and_resume(db_session):
    db = db_session
    org, _, service, user, branch = await world(db)
    tickets = []
    for status in (TicketStatus.WAITING, TicketStatus.CALLED, TicketStatus.SERVING,
                   TicketStatus.DONE, TicketStatus.CANCELLED, TicketStatus.ABSENT):
        distinct_user = User(phone="+257" + str(uuid.uuid4().int % 10**8).zfill(8))
        db.add(distinct_user); await db.commit()
        tickets.append(await ticket_with_history(db, org, branch, service, distinct_user, status))
    first = await run_backfill(db, dry_run=False, batch_size=2, max_batches=1)
    assert first.inserted == 2 and first.last_id
    resumed = await run_backfill(db, dry_run=False, batch_size=2, after_id=first.last_id,
                                 as_of=datetime.fromisoformat(first.as_of))
    assert resumed.inserted == 4
    assert len((await db.scalars(select(AbuseEvent))).all()) == 6
    assert (await run_backfill(db, dry_run=False)).inserted == 0
