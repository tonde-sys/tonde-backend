"""Production AWTE persistence and decision rules."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.branch import Branch, Service
from app.models.counter import Counter
from app.models.eta import EtaCounterProfile, EtaObservation, EtaPrediction, EtaServiceProfile
from app.models.organization import Organization
from app.models.ticket import Ticket, TicketPriority, TicketStatus
from app.services.eta_service import EtaEstimator, ewma_step
from app.services.ticket_service import TicketService


async def _make_world(db, *, avg_minutes: int = 6):
    org = Organization(name="Banque", slug=f"org-{uuid.uuid4().hex[:10]}", sector="bank")
    db.add(org)
    await db.flush()
    branch = Branch(
        org_id=org.id,
        name="Centre",
        slug=f"branch-{uuid.uuid4().hex[:10]}",
        sector="bank",
        is_active=True,
        is_open=True,
        opens_at="00:00",
        closes_at="23:59",
    )
    db.add(branch)
    await db.flush()
    service = Service(
        org_id=org.id,
        branch_id=branch.id,
        name="Caisse",
        ticket_prefix="A",
        avg_duration_minutes=avg_minutes,
        eta_min_service_samples=3,
        eta_min_counter_samples=10,
        eta_outlier_min_samples=3,
    )
    db.add(service)
    await db.flush()
    counter = Counter(
        org_id=org.id,
        agency_id=branch.id,
        name="Guichet 1",
        is_open=True,
        is_active=True,
    )
    db.add(counter)
    await db.flush()
    return org, branch, service, counter


async def _make_ticket(db, org, branch, service, counter, *, sequence=1, status=TicketStatus.WAITING,
                       served_at=None, done_at=None):
    ticket = Ticket(
        number=f"A-{sequence:03d}",
        prefix="A",
        sequence=sequence,
        queue_sequence=sequence,
        user_id=str(uuid.uuid4()),
        org_id=org.id,
        agency_id=branch.id,
        service_id=service.id,
        counter_id=counter.id if counter else None,
        status=status,
        priority=TicketPriority.STANDARD,
        served_at=served_at,
        done_at=done_at,
    )
    db.add(ticket)
    await db.flush()
    return ticket


def test_ewma_converges_from_the_configured_duration():
    value = 600.0
    for observed in (360, 480, 420):
        value = ewma_step(value, observed, alpha=0.25)
    assert value == pytest.approx(498.75)


@pytest.mark.asyncio
async def test_completion_persists_once_and_updates_service_and_counter_profiles(db_session):
    org, branch, service, counter = await _make_world(db_session)
    now = datetime.now(timezone.utc)
    ticket = await _make_ticket(
        db_session, org, branch, service, counter,
        status=TicketStatus.DONE,
        served_at=now - timedelta(seconds=300),
        done_at=now,
    )

    estimator = EtaEstimator(db_session)
    assert await estimator.record_completed_ticket(ticket) is True
    await db_session.commit()
    assert await estimator.record_completed_ticket(ticket) is False

    observation = await db_session.scalar(
        select(EtaObservation).where(EtaObservation.ticket_id == ticket.id)
    )
    profile = await db_session.get(EtaServiceProfile, service.id)
    counter_profile = await db_session.scalar(
        select(EtaCounterProfile).where(
            EtaCounterProfile.service_id == service.id,
            EtaCounterProfile.counter_id == counter.id,
        )
    )
    assert observation is not None and observation.eligible is True
    assert observation.duration_seconds == 300
    assert profile.sample_count == 1
    assert profile.ewma_seconds == pytest.approx(345)
    assert counter_profile.sample_count == 1
    assert counter_profile.ewma_seconds == pytest.approx(345)


@pytest.mark.asyncio
async def test_completion_does_not_attach_foreign_tenant_counter(db_session):
    org, branch, service, _counter = await _make_world(db_session)
    foreign_org, _foreign_branch, _foreign_service, foreign_counter = await _make_world(db_session)
    now = datetime.now(timezone.utc)
    ticket = await _make_ticket(
        db_session, org, branch, service, foreign_counter,
        status=TicketStatus.DONE,
        served_at=now - timedelta(seconds=300),
        done_at=now,
    )

    assert await EtaEstimator(db_session).record_completed_ticket(ticket) is True
    observation = await db_session.scalar(
        select(EtaObservation).where(EtaObservation.ticket_id == ticket.id)
    )
    assert foreign_org.id != org.id
    assert observation.counter_id is None
    assert await db_session.scalar(
        select(EtaCounterProfile.id).where(EtaCounterProfile.service_id == service.id)
    ) is None


@pytest.mark.asyncio
async def test_mad_outlier_is_retained_but_excluded_from_ewma(db_session):
    org, branch, service, counter = await _make_world(db_session)
    now = datetime.now(timezone.utc)
    for index, duration in enumerate((290, 295, 300, 305, 310)):
        prior = await _make_ticket(
            db_session, org, branch, service, counter,
            sequence=index + 1,
            status=TicketStatus.DONE,
            served_at=now - timedelta(seconds=duration),
            done_at=now,
        )
        db_session.add(EtaObservation(
            org_id=org.id,
            branch_id=branch.id,
            service_id=service.id,
            counter_id=counter.id,
            ticket_id=prior.id,
            event_key=f"seed:{index}",
            duration_seconds=duration,
            eligible=True,
            observed_at=now - timedelta(minutes=10 - index),
            service_started_at=now - timedelta(minutes=20),
            service_completed_at=now - timedelta(minutes=10),
        ))
    await db_session.flush()
    ticket = await _make_ticket(
        db_session, org, branch, service, counter,
        sequence=99,
        status=TicketStatus.DONE,
        served_at=now - timedelta(seconds=1800),
        done_at=now,
    )

    assert await EtaEstimator(db_session).record_completed_ticket(ticket) is False
    observation = await db_session.scalar(
        select(EtaObservation).where(EtaObservation.ticket_id == ticket.id)
    )
    assert observation.eligible is False
    assert observation.exclusion_reason == "robust_mad_outlier"
    assert observation.duration_seconds == 1800
    assert await db_session.get(EtaServiceProfile, service.id) is None


@pytest.mark.asyncio
async def test_profile_threshold_and_paused_counter_control_eta(db_session):
    org, branch, service, counter = await _make_world(db_session, avg_minutes=6)
    waiting = await _make_ticket(db_session, org, branch, service, counter)
    db_session.add(EtaServiceProfile(
        service_id=service.id,
        org_id=org.id,
        ewma_seconds=120,
        sample_count=3,
    ))
    await db_session.commit()

    estimate = (await EtaEstimator(db_session).estimate_many([service], persist_forecasts=True))[service.id]
    await db_session.commit()
    assert estimate.ticket_minutes[waiting.id] == 0
    assert estimate.next_ticket_minutes == 2
    assert estimate.next_ticket.profile_level == "service"
    assert estimate.next_ticket.confidence == "medium"

    counter.is_paused = True
    await db_session.commit()
    paused = (await EtaEstimator(db_session).estimate_many([service], persist_forecasts=True))[service.id]
    await db_session.commit()
    assert paused.next_ticket_minutes is None
    assert paused.next_ticket.confidence == "unavailable"
    predictions = (await db_session.execute(
        select(EtaPrediction).where(EtaPrediction.ticket_id == waiting.id).order_by(EtaPrediction.version)
    )).scalars().all()
    assert [prediction.version for prediction in predictions] == [1, 2]
    assert predictions[0].estimated_wait_seconds == 0
    assert predictions[1].estimated_wait_seconds is None
    assert predictions[1].confidence == "unavailable"


@pytest.mark.asyncio
async def test_latest_forecast_is_evaluated_against_actual_remaining_wait(db_session):
    org, branch, service, counter = await _make_world(db_session)
    ticket = await _make_ticket(db_session, org, branch, service, counter)
    calculated_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    db_session.add(EtaPrediction(
        ticket_id=ticket.id,
        org_id=org.id,
        branch_id=branch.id,
        service_id=service.id,
        estimated_wait_seconds=90,
        profile_level="service",
        confidence="medium",
        sample_count=3,
        calculated_at=calculated_at,
    ))
    await db_session.flush()

    await EtaEstimator(db_session).record_prediction_at_call(ticket, datetime.now(timezone.utc))
    await db_session.commit()
    prediction = await db_session.scalar(
        select(EtaPrediction).where(EtaPrediction.ticket_id == ticket.id).order_by(EtaPrediction.version.desc())
    )
    metrics = await EtaEstimator(db_session).metrics(org_id=org.id, service_id=service.id)

    assert metrics["evaluated_forecasts"] == 1
    assert metrics["mae_seconds"] == pytest.approx(30, abs=1)
    assert prediction.counter_id == counter.id
    assert metrics["by_counter"][0]["counter_id"] == counter.id
    assert metrics["by_counter"][0]["mae_seconds"] == pytest.approx(30, abs=1)


@pytest.mark.asyncio
async def test_estimator_failure_returns_no_fabricated_position_eta(db_session, monkeypatch):
    org, branch, service, counter = await _make_world(db_session)
    ticket = await _make_ticket(db_session, org, branch, service, counter)

    async def fail(*args, **kwargs):
        raise RuntimeError("database projection unavailable")

    monkeypatch.setattr(EtaEstimator, "estimate_many", fail)
    value = await TicketService(db_session).estimate_ticket_wait_minutes(ticket, service)

    assert value is None
