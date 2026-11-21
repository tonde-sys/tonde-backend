"""A late historical insert behind a saved UUID cursor must be found by final full scan."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.qape import AbuseEvent
from app.models.ticket import TicketStatus
from app.services.qape_backfill import run_backfill
from tests.test_qape_data import world
from tests.test_qape_backfill import ticket_with_history


@pytest.mark.asyncio
async def test_late_backdated_ticket_behind_cursor_requires_reconciliation(db_session):
    db = db_session
    org, _, service, user, branch = await world(db)
    high = await ticket_with_history(db, org, branch, service, user, TicketStatus.DONE,
                                     ticket_id="ffffffff-ffff-ffff-ffff-ffffffffffff")
    first = await run_backfill(db, dry_run=False, batch_size=1, max_batches=1)
    assert first.inserted == 1 and first.reconciliation_required
    low = await ticket_with_history(db, org, branch, service, user, TicketStatus.CANCELLED,
                                    ticket_id="00000000-0000-0000-0000-000000000001")
    resumed = await run_backfill(db, dry_run=False, after_id=first.last_id,
                                 as_of=datetime.fromisoformat(first.as_of))
    assert resumed.inserted == 0 and resumed.reconciliation_required
    reconciliation = await run_backfill(db, dry_run=True)
    assert reconciliation.recoverable == 1 and reconciliation.certain == 1
    assert reconciliation.reconciliation_required
    assert len((await db.scalars(select(AbuseEvent))).all()) == 1
    repair = await run_backfill(db, dry_run=False, as_of=datetime.fromisoformat(reconciliation.as_of))
    assert repair.inserted == 1 and repair.reconciliation_required
    verified = await run_backfill(db, dry_run=True)
    assert verified.complete_scan and not verified.reconciliation_required
    assert verified.recoverable == 0 and verified.already_recorded == 2
    assert len((await db.scalars(select(AbuseEvent))).all()) == 2
