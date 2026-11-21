"""Additional evidence classification and invalid provenance cases."""
from datetime import datetime, timedelta, timezone
import json
import uuid

import pytest
from sqlalchemy import select

from app.models.branch import Service
from app.models.queue_log import QueueLog, QueueLogAction
from app.models.qape import AbuseEvent
from app.models.ticket import Ticket, TicketStatus
from app.services.qape_backfill import run_backfill, _origin
from tests.test_qape_data import world


@pytest.mark.asyncio
async def test_inferred_origin_corroborated_by_prior_log(db_session):
    db = db_session
    org, _, source, user, branch = await world(db)
    target = Service(name="Dest", org_id=org.id, branch_id=branch.id)
    db.add(target); await db.flush()
    when = datetime.now(timezone.utc)-timedelta(days=1)
    ticket = Ticket(user_id=user.id, org_id=org.id, agency_id=branch.id,
                    service_id=target.id, number="B-001", sequence=1,
                    queue_sequence=1, created_at=when, status=TicketStatus.DONE)
    db.add(ticket); await db.flush()
    db.add(QueueLog(org_id=org.id, agency_id=branch.id, service_id=source.id,
                    ticket_id=ticket.id, action=QueueLogAction.TICKET_CALLED,
                    to_status="called", created_at=when+timedelta(minutes=1)))
    db.add(QueueLog(org_id=org.id, agency_id=branch.id, service_id=source.id,
                    ticket_id=ticket.id, action=QueueLogAction.TICKET_TRANSFERRED,
                    to_status="transferred", created_at=when+timedelta(minutes=2),
                    extra_data=json.dumps({"from_service_id": source.id, "to_service_id": target.id})))
    await db.commit()
    report = await run_backfill(db, dry_run=False)
    assert report.inserted == 1 and len(report.inferred) == 1
    event = (await db.scalars(select(AbuseEvent))).one()
    assert event.service_id == source.id and event.decision is None
    assert event.provenance == "HISTORICAL_INFERRED"
    from app.services.qape_data import EventRepository
    start, end = when-timedelta(days=1), datetime.now(timezone.utc)
    assert await EventRepository(db).count_created(org.id, source.id, start, end) == 0


@pytest.mark.asyncio
async def test_broken_transfer_chain_is_excluded(db_session):
    db = db_session
    org, _, source, user, branch = await world(db)
    target = Service(name="Dest", org_id=org.id, branch_id=branch.id)
    db.add(target); await db.flush()
    when = datetime.now(timezone.utc)-timedelta(days=1)
    ticket = Ticket(user_id=user.id, org_id=org.id, agency_id=branch.id,
                    service_id=target.id, number="B-001", sequence=1,
                    queue_sequence=1, created_at=when, status=TicketStatus.DONE)
    db.add(ticket); await db.flush()
    db.add(QueueLog(org_id=org.id, agency_id=branch.id, service_id=source.id,
                    ticket_id=ticket.id, action=QueueLogAction.TICKET_CREATED,
                    to_status="waiting", created_at=when+timedelta(seconds=1)))
    db.add(QueueLog(org_id=org.id, agency_id=branch.id, service_id=source.id,
                    ticket_id=ticket.id, action=QueueLogAction.TICKET_TRANSFERRED,
                    to_status="transferred", created_at=when+timedelta(minutes=2),
                    extra_data=json.dumps({"from_service_id": source.id, "to_service_id": "nonexistent"})))
    await db.commit()
    report = await run_backfill(db, dry_run=False)
    assert report.inserted == 0 and report.ambiguous[0]["reason"] == "broken_transfer_chain"
