"""Contrat ETA/pause : heure de reprise annoncée et apprentissage configurable."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.branch import Branch, Service
from app.models.counter import Counter
from app.models.eta import EtaCounterProfile, EtaServiceProfile
from app.models.organization import Organization
from app.schemas.counter import PauseCounterRequest
from app.schemas.service import UpdateServiceRequest
from app.services.counter_service import CounterService
from app.services.service_service import ServiceService
from app.websocket.events import EventType
from app.websocket.publisher import queue_snapshot_event
from app.websocket.queue_ws import QueueWebSocketManager


async def _seed(db: AsyncSession):
    org = Organization(name="ETA Test", slug=f"eta-test-{datetime.now().timestamp()}", sector="bank")
    db.add(org)
    await db.flush()
    branch = Branch(
        org_id=org.id,
        name="Agence",
        slug=f"agence-{datetime.now().timestamp()}",
        sector="bank",
        is_open=True,
    )
    db.add(branch)
    await db.flush()
    service = Service(
        org_id=org.id,
        branch_id=branch.id,
        name="Caisse",
        ticket_prefix="A",
        avg_duration_minutes=5,
    )
    counter = Counter(
        org_id=org.id,
        agency_id=branch.id,
        name="Guichet 1",
        is_open=True,
        is_active=True,
    )
    db.add_all([service, counter])
    await db.commit()
    return org, branch, service, counter


def test_pause_request_rejects_past_resume_time():
    with pytest.raises(ValidationError):
        PauseCounterRequest(paused_until=datetime.now(timezone.utc) - timedelta(minutes=1))


@pytest.mark.asyncio
async def test_pause_stores_expected_resume_time_and_resume_clears_it(db_session, mock_redis):
    org, branch, _service, counter = await _seed(db_session)
    service = CounterService(db_session)
    until = datetime.now(timezone.utc) + timedelta(minutes=35)

    paused = await service.pause(
        counter.id,
        org.id,
        agency_id=branch.id,
        data=PauseCounterRequest(paused_until=until),
    )
    assert paused.is_paused is True
    assert paused.paused_until is not None
    stored = paused.paused_until
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    assert abs((stored - until).total_seconds()) < 2

    resumed = await service.resume(counter.id, org.id, agency_id=branch.id)
    assert resumed.is_paused is False
    assert resumed.paused_until is None


@pytest.mark.asyncio
async def test_admin_duration_change_reseeds_ewma_without_losing_samples(db_session):
    org, _branch, service, _counter = await _seed(db_session)
    db_session.add(EtaServiceProfile(
        service_id=service.id, org_id=org.id, ewma_seconds=180.0, sample_count=12,
    ))
    db_session.add(EtaCounterProfile(
        service_id=service.id,
        org_id=org.id,
        counter_id="counter-1",
        ewma_seconds=190.0,
        sample_count=4,
    ))
    await db_session.commit()

    updated = await ServiceService(db_session).update(
        service.id,
        service.branch_id,
        org.id,
        UpdateServiceRequest(avg_duration_minutes=9),
    )
    assert updated["avg_duration_minutes"] == 9

    profiles = (await db_session.execute(
        select(EtaServiceProfile, EtaCounterProfile)
        .join(
            EtaCounterProfile,
            EtaCounterProfile.service_id == EtaServiceProfile.service_id,
        )
    )).all()
    for service_profile, counter_profile in profiles:
        assert service_profile.ewma_seconds == 540.0
        assert counter_profile.ewma_seconds == 540.0
        assert service_profile.sample_count == 12
        assert counter_profile.sample_count == 4


@pytest.mark.asyncio
async def test_paused_counters_reach_mobile_queue_update():
    manager = QueueWebSocketManager()
    ws = MagicMock()
    ws.send_text = AsyncMock()
    manager.connections_by_ticket = {"t-1": {ws}}
    paused_until = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()

    event = queue_snapshot_event(
        agency_id="a-1",
        service_id="s-1",
        positions={"t-1": 1},
        current_called_number="A-001",
        eta_seconds_per_position=300,
        eta_version=1,
        paused_counters=[{"name": "Guichet 1", "paused_until": paused_until}],
    )
    await manager._dispatch_event(event)

    message = json.loads(ws.send_text.call_args.args[0])
    assert message["type"] == EventType.QUEUE_UPDATE.value
    assert message["paused_counters"][0]["name"] == "Guichet 1"
    assert message["paused_counters"][0]["paused_until"] == paused_until
