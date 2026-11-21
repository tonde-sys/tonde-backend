"""Historical origin missing from service catalogue remains excluded."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.models.queue_log import QueueLogAction
from app.services.qape_backfill import _origin


def test_origin_service_not_found_is_ambiguous():
    now = datetime.now(timezone.utc)
    ticket = SimpleNamespace(org_id="org", service_id="current", created_at=now)
    log = SimpleNamespace(action=QueueLogAction.TICKET_CREATED, org_id="org",
                          service_id="deleted-service", created_at=now+timedelta(seconds=1))
    assert _origin(ticket, [log], {}) == (None, "ambiguous", "origin_service_missing_or_foreign")
