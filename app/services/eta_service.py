"""Production ETA calculation from persisted operations and live queue state.

The forecast is a workload projection over real WAITING/CALLED/SERVING tickets.
The engine never inserts, reorders or removes queue tickets. PostgreSQL stores
observations, EWMA profiles and the latest delivered forecast; Redis remains the
operational queue projection and can be rebuilt by the existing Queue Engine.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.branch import Branch, Service
from app.models.counter import Counter
from app.models.eta import EtaCounterProfile, EtaObservation, EtaPrediction, EtaServiceProfile
from app.models.queue_log import QueueLog, QueueLogAction
from app.models.ticket import PRIORITY_SCORES, Ticket, TicketStatus


@dataclass(frozen=True)
class EtaValue:
    wait_seconds: int | None
    confidence: str
    profile_level: str
    sample_count: int


@dataclass(frozen=True)
class ServiceQueueEstimate:
    """Individual waiting-ticket forecasts and a forecast for the next arrival."""

    ticket_minutes: dict[str, int]
    next_ticket_minutes: int | None
    ticket_details: dict[str, EtaValue] = field(default_factory=dict)
    next_ticket: EtaValue = EtaValue(None, "unavailable", "none", 0)
    calculated_at: datetime | None = None
    version: int = 0


@dataclass
class _Lane:
    counter_id: str
    free_in_seconds: int = 0
    confidence_penalty: bool = False


class EtaEstimator:
    """Estimate time-to-call using tenant-scoped EWMA profiles and queue state."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def estimate_many(
        self,
        services: list[Service],
        *,
        now: datetime | None = None,
        persist_forecasts: bool = False,
    ) -> dict[str, ServiceQueueEstimate]:
        requested_targets = {service.id: service for service in services}
        if not requested_targets:
            return {}
        calculated_at = _as_utc(now or datetime.now(timezone.utc))
        branch_ids = {service.branch_id for service in services}
        org_ids = {service.org_id for service in services if service.org_id}
        if not org_ids:
            return {service_id: _unavailable(calculated_at) for service_id in requested_targets}

        # Branch and service tenant ownership must both match. Legacy service rows
        # without a tenant are unavailable until repaired; we do not infer one
        # from an untrusted caller-supplied object.
        catalog_rows = (await self.db.execute(
            select(Service)
            .join(Branch, Branch.id == Service.branch_id)
            .where(
                Branch.id.in_(branch_ids),
                Branch.org_id.in_(org_ids),
                Service.org_id == Branch.org_id,
            )
        )).scalars().all()
        catalog = {service.id: service for service in catalog_rows}
        targets = {service_id: catalog[service_id] for service_id in requested_targets if service_id in catalog}
        if not targets:
            return {service_id: _unavailable(calculated_at) for service_id in requested_targets}
        invalid_target_ids = set(requested_targets) - set(targets)
        service_ids = list(catalog)

        waiting_rows = (await self.db.execute(
            select(Ticket).where(
                Ticket.org_id.in_(org_ids),
                Ticket.agency_id.in_(branch_ids),
                Ticket.service_id.in_(targets),
                Ticket.status == TicketStatus.WAITING,
            )
        )).scalars().all()
        waiting_by_service: dict[str, list[Ticket]] = {key: [] for key in targets}
        for ticket in waiting_rows:
            waiting_by_service.setdefault(ticket.service_id, []).append(ticket)
        for queue in waiting_by_service.values():
            queue.sort(key=lambda ticket: (
                -PRIORITY_SCORES[ticket.priority],
                ticket.queue_sequence,
                _as_utc(ticket.created_at),
                ticket.id,
            ))

        counters = (await self.db.execute(
            select(Counter)
            .where(
                Counter.org_id.in_(org_ids),
                Counter.agency_id.in_(branch_ids),
                Counter.is_active.is_(True),
                Counter.is_open.is_(True),
                Counter.is_paused.is_(False),
            )
            .order_by(Counter.name, Counter.id)
        )).scalars().all()
        counters_by_branch: dict[str, list[Counter]] = {key: [] for key in branch_ids}
        for counter in counters:
            counters_by_branch.setdefault(counter.agency_id, []).append(counter)

        current_ids = list({counter.current_ticket_id for counter in counters if counter.current_ticket_id})
        current_by_id: dict[str, Ticket] = {}
        if current_ids:
            current_rows = (await self.db.execute(
                select(Ticket).where(
                    Ticket.id.in_(current_ids),
                    Ticket.org_id.in_(org_ids),
                    Ticket.agency_id.in_(branch_ids),
                    Ticket.status.in_([TicketStatus.CALLED, TicketStatus.SERVING]),
                )
            )).scalars().all()
            current_by_id = {ticket.id: ticket for ticket in current_rows}

        counter_ids = [counter.id for counter in counters]
        profile_join = and_(
            EtaCounterProfile.org_id.in_(org_ids),
            EtaCounterProfile.counter_id.in_(counter_ids) if counter_ids else EtaCounterProfile.id == "",
            EtaCounterProfile.service_id == Service.id,
        )
        profile_rows = (await self.db.execute(
            select(Service.id, EtaServiceProfile, EtaCounterProfile)
            .join(Branch, Branch.id == Service.branch_id)
            .outerjoin(
                EtaServiceProfile,
                and_(
                    EtaServiceProfile.org_id == Branch.org_id,
                    EtaServiceProfile.service_id == Service.id,
                ),
            )
            .outerjoin(EtaCounterProfile, profile_join)
            .where(Branch.id.in_(branch_ids), Branch.org_id.in_(org_ids), Service.id.in_(service_ids))
        )).all()
        service_profile_by_id: dict[str, EtaServiceProfile] = {}
        counter_profile_by_key: dict[tuple[str, str], EtaCounterProfile] = {}
        for service_id, service_profile, counter_profile in profile_rows:
            if service_profile is not None:
                service_profile_by_id[service_id] = service_profile
            if counter_profile is not None:
                counter_profile_by_key[(service_id, counter_profile.counter_id)] = counter_profile

        service_samples, counter_samples = await self._load_recent_samples(org_ids, service_ids)
        serving_actor_by_ticket: dict[str, str] = {}
        if current_ids:
            actor_rows = (await self.db.execute(
                select(QueueLog.ticket_id, QueueLog.actor_id)
                .where(
                    QueueLog.org_id.in_(org_ids),
                    QueueLog.ticket_id.in_(current_ids),
                    QueueLog.action == QueueLogAction.TICKET_SERVING,
                    QueueLog.actor_id.is_not(None),
                )
                .order_by(QueueLog.created_at.desc())
            )).all()
            for ticket_id, actor_id in actor_rows:
                serving_actor_by_ticket.setdefault(ticket_id, actor_id)

        # There is currently one independently ordered queue per service. Since
        # the Queue Engine does not define cross-service dispatch fairness, an
        # open counter is projected against this service's real queue and the
        # confidence is reduced when competing service queues exist.
        competing_rows = (await self.db.execute(
            select(Ticket.agency_id, func.count(Ticket.id))
            .where(
                Ticket.org_id.in_(org_ids),
                Ticket.agency_id.in_(branch_ids),
                Ticket.status == TicketStatus.WAITING,
                Ticket.service_id.not_in(targets),
            )
            .group_by(Ticket.agency_id)
        )).all()
        competing_by_branch = {branch_id: count for branch_id, count in competing_rows}

        results: dict[str, ServiceQueueEstimate] = {
            service_id: _unavailable(calculated_at)
            for service_id in invalid_target_ids
        }
        version = int(calculated_at.timestamp() * 1000)
        for service_id, target in targets.items():
            lanes: list[_Lane] = []
            for counter in counters_by_branch.get(target.branch_id, []):
                current = current_by_id.get(counter.current_ticket_id or "")
                free_in = 0
                penalty = bool(competing_by_branch.get(target.branch_id))
                if current is not None:
                    current_service = catalog.get(current.service_id)
                    if current_service is None:
                        # Unknown work occupying this resource is not treated as free capacity.
                        continue
                    if current.status == TicketStatus.CALLED:
                        # No arbitrary call-to-service delay: actual processing starts
                        # at `served_at`. The unknown handoff is reflected in confidence.
                        free_in = self._duration_for(
                            current_service, counter.id, service_profile_by_id,
                            counter_profile_by_key,
                        )[0]
                        penalty = True
                    elif current.served_at:
                        elapsed = max(0, int((calculated_at - _as_utc(current.served_at)).total_seconds()))
                        duration, _level, _count = self._duration_for(
                            current_service, counter.id, service_profile_by_id,
                            counter_profile_by_key,
                        )
                        observed = counter_samples.get((current.service_id, counter.id)) or service_samples.get(current.service_id, [])
                        remaining = [sample - elapsed for sample in observed if sample > elapsed]
                        if remaining:
                            free_in = int(round(sum(remaining) / len(remaining)))
                        elif observed:
                            # The service has exceeded all comparable samples. The
                            # conditional tail is unobserved, so use the learned mean
                            # (or configured mean when data is insufficient) and lower confidence.
                            free_in = int(round(duration))
                            penalty = True
                        else:
                            free_in = max(0, int(round(duration)) - elapsed)
                            penalty = True
                    else:
                        free_in = int(round(self._duration_for(
                            current_service, counter.id, service_profile_by_id,
                            counter_profile_by_key,
                        )[0]))
                        penalty = True
                lanes.append(_Lane(counter.id, max(0, free_in), penalty))

            if not lanes:
                result = _unavailable(calculated_at, version)
                results[service_id] = result
                if persist_forecasts:
                    await self._persist_forecasts(target, waiting_by_service.get(service_id, []), result)
                continue

            ticket_details: dict[str, EtaValue] = {}
            ticket_minutes: dict[str, int] = {}
            for ticket in waiting_by_service.get(service_id, []):
                lane = min(lanes, key=lambda item: (item.free_in_seconds, item.counter_id))
                seconds, level, count = self._duration_for(
                    target, lane.counter_id, service_profile_by_id, counter_profile_by_key
                )
                confidence = _confidence(
                    level, count, lane.confidence_penalty,
                    target.eta_min_service_samples, target.eta_min_counter_samples,
                )
                value = EtaValue(lane.free_in_seconds, confidence, level, count)
                ticket_details[ticket.id] = value
                ticket_minutes[ticket.id] = _ceil_minutes(lane.free_in_seconds)
                lane.free_in_seconds += max(0, int(round(seconds)))

            next_lane = min(lanes, key=lambda item: (item.free_in_seconds, item.counter_id))
            next_seconds, next_level, next_count = self._duration_for(
                target, next_lane.counter_id, service_profile_by_id, counter_profile_by_key
            )
            next_value = EtaValue(
                next_lane.free_in_seconds,
                _confidence(
                    next_level, next_count, next_lane.confidence_penalty,
                    target.eta_min_service_samples, target.eta_min_counter_samples,
                ),
                next_level,
                next_count,
            )
            result = ServiceQueueEstimate(
                ticket_minutes=ticket_minutes,
                next_ticket_minutes=(
                    _ceil_minutes(next_value.wait_seconds)
                    if next_value.wait_seconds is not None else None
                ),
                ticket_details=ticket_details,
                next_ticket=next_value,
                calculated_at=calculated_at,
                version=version,
            )
            results[service_id] = result
            if persist_forecasts:
                await self._persist_forecasts(target, waiting_by_service.get(service_id, []), result)
        return results

    async def _load_recent_samples(
        self, org_ids: set[str], service_ids: list[str]
    ) -> tuple[dict[str, list[int]], dict[tuple[str, str], list[int]]]:
        if not service_ids:
            return {}, {}
        ranked_service = (
            select(
                EtaObservation.service_id.label("service_id"),
                EtaObservation.duration_seconds.label("duration_seconds"),
                func.row_number().over(
                    partition_by=EtaObservation.service_id,
                    order_by=(EtaObservation.observed_at.desc(), EtaObservation.id.desc()),
                ).label("rn"),
            )
            .where(
                EtaObservation.org_id.in_(org_ids),
                EtaObservation.service_id.in_(service_ids),
                EtaObservation.eligible.is_(True),
            )
            .subquery()
        )
        service_rows = (await self.db.execute(
            select(ranked_service.c.service_id, ranked_service.c.duration_seconds)
            .where(ranked_service.c.rn <= 100)
        )).all()
        service_samples: dict[str, list[int]] = {}
        for service_id, seconds in service_rows:
            service_samples.setdefault(service_id, []).append(seconds)

        ranked_counter = (
            select(
                EtaObservation.service_id.label("service_id"),
                EtaObservation.counter_id.label("counter_id"),
                EtaObservation.duration_seconds.label("duration_seconds"),
                func.row_number().over(
                    partition_by=(EtaObservation.service_id, EtaObservation.counter_id),
                    order_by=(EtaObservation.observed_at.desc(), EtaObservation.id.desc()),
                ).label("rn"),
            )
            .where(
                EtaObservation.org_id.in_(org_ids),
                EtaObservation.service_id.in_(service_ids),
                EtaObservation.counter_id.is_not(None),
                EtaObservation.eligible.is_(True),
            )
            .subquery()
        )
        counter_rows = (await self.db.execute(
            select(ranked_counter.c.service_id, ranked_counter.c.counter_id, ranked_counter.c.duration_seconds)
            .where(ranked_counter.c.rn <= 100)
        )).all()
        counter_samples: dict[tuple[str, str], list[int]] = {}
        for service_id, counter_id, seconds in counter_rows:
            counter_samples.setdefault((service_id, counter_id), []).append(seconds)
        return service_samples, counter_samples

    @staticmethod
    def _duration_for(
        service: Service,
        counter_id: str,
        service_profiles: dict[str, EtaServiceProfile],
        counter_profiles: dict[tuple[str, str], EtaCounterProfile],
    ) -> tuple[float, str, int]:
        configured = float(service.avg_duration_minutes * 60)
        service_profile = service_profiles.get(service.id)
        service_count = service_profile.sample_count if service_profile else 0
        service_seconds = service_profile.ewma_seconds if service_profile else configured
        if service_count >= service.eta_min_service_samples:
            service_level = "service"
        else:
            service_level = "configured"
            service_seconds = configured

        local = counter_profiles.get((service.id, counter_id))
        if local and local.sample_count >= service.eta_min_counter_samples:
            return local.ewma_seconds, "counter", local.sample_count
        if service_level == "service":
            return service_seconds, "service", service_count
        return configured, "configured", service_count

    async def record_completed_ticket(self, ticket: Ticket) -> bool:
        """Persist one raw completion and update service/counter EWMA atomically."""
        if not ticket.org_id:
            return False
        service = (await self.db.execute(
            select(Service).where(
                Service.id == ticket.service_id,
                Service.org_id == ticket.org_id,
                Service.branch_id == ticket.agency_id,
            ).with_for_update()
        )).scalar_one_or_none()
        if service is None:
            return False
        counter_id = None
        if ticket.counter_id:
            counter_id = await self.db.scalar(select(Counter.id).where(
                Counter.id == ticket.counter_id,
                Counter.org_id == ticket.org_id,
                Counter.agency_id == ticket.agency_id,
            ))
        event_key = f"done:{ticket.id}"
        existing = await self.db.scalar(select(EtaObservation.id).where(
            EtaObservation.org_id == ticket.org_id,
            EtaObservation.event_key == event_key,
        ))
        if existing:
            return False

        started = _as_utc(ticket.served_at) if ticket.served_at else None
        completed = _as_utc(ticket.done_at) if ticket.done_at else None
        duration = int((completed - started).total_seconds()) if started and completed else None
        eligible = duration is not None and duration > 0
        reason = None
        if not eligible:
            reason = "missing_or_invalid_service_timestamps" if duration is None else "non_positive_duration"
        elif await self._is_outlier(service, duration):
            eligible = False
            reason = "robust_mad_outlier"

        observation = EtaObservation(
            org_id=ticket.org_id,
            branch_id=ticket.agency_id,
            service_id=ticket.service_id,
            counter_id=counter_id,
            ticket_id=ticket.id,
            event_key=event_key,
            duration_seconds=duration,
            eligible=eligible,
            exclusion_reason=reason,
            observed_at=completed or datetime.now(timezone.utc),
            service_started_at=started,
            service_completed_at=completed,
        )
        self.db.add(observation)
        if not eligible:
            await self.db.flush()
            return False

        assert duration is not None
        service_profile = (await self.db.execute(
            select(EtaServiceProfile)
            .where(EtaServiceProfile.org_id == ticket.org_id, EtaServiceProfile.service_id == service.id)
            .with_for_update()
        )).scalar_one_or_none()
        if service_profile is None:
            service_profile = EtaServiceProfile(
                service_id=service.id,
                org_id=ticket.org_id,
                ewma_seconds=float(service.avg_duration_minutes * 60),
                sample_count=0,
            )
            self.db.add(service_profile)
            await self.db.flush()
        service_profile.ewma_seconds = ewma_step(service_profile.ewma_seconds, duration, service.eta_ewma_alpha)
        service_profile.sample_count += 1
        service_profile.updated_at = completed or datetime.now(timezone.utc)

        if counter_id:
            counter_profile = (await self.db.execute(
                select(EtaCounterProfile)
                .where(
                    EtaCounterProfile.org_id == ticket.org_id,
                    EtaCounterProfile.service_id == service.id,
                    EtaCounterProfile.counter_id == counter_id,
                )
                .with_for_update()
            )).scalar_one_or_none()
            if counter_profile is None:
                counter_profile = EtaCounterProfile(
                    org_id=ticket.org_id,
                    service_id=service.id,
                    counter_id=counter_id,
                    ewma_seconds=float(service.avg_duration_minutes * 60),
                    sample_count=0,
                )
                self.db.add(counter_profile)
                await self.db.flush()
            counter_profile.ewma_seconds = ewma_step(counter_profile.ewma_seconds, duration, service.eta_ewma_alpha)
            counter_profile.sample_count += 1
            counter_profile.updated_at = completed or datetime.now(timezone.utc)
        await self.db.flush()
        return True

    async def record_interrupted_ticket(self, ticket: Ticket, reason: str) -> bool:
        """Retain a real but ineligible lifecycle observation for audit."""
        if not ticket.org_id:
            return False
        action_by_reason = {
            "incomplete": QueueLogAction.TICKET_INCOMPLETE,
            "transferred": QueueLogAction.TICKET_TRANSFERRED,
            "absent": QueueLogAction.TICKET_ABSENT,
        }
        action = action_by_reason.get(reason)
        if action is None:
            return False
        log = (await self.db.execute(
            select(QueueLog.id, QueueLog.created_at)
            .where(
                QueueLog.org_id == ticket.org_id,
                QueueLog.ticket_id == ticket.id,
                QueueLog.service_id == ticket.service_id,
                QueueLog.action == action,
            )
            .order_by(QueueLog.created_at.desc(), QueueLog.id.desc())
            .limit(1)
        )).first()
        if log is None:
            return False
        event_key = f"queue-log:{log.id}"
        exists = await self.db.scalar(select(EtaObservation.id).where(
            EtaObservation.org_id == ticket.org_id,
            EtaObservation.event_key == event_key,
        ))
        if exists:
            return False
        ended_at = _as_utc(log.created_at)
        started_at = _as_utc(ticket.served_at) if ticket.served_at else None
        duration = int((ended_at - started_at).total_seconds()) if started_at else None
        counter_id = None
        if ticket.counter_id:
            counter_id = await self.db.scalar(select(Counter.id).where(
                Counter.id == ticket.counter_id,
                Counter.org_id == ticket.org_id,
                Counter.agency_id == ticket.agency_id,
            ))
        self.db.add(EtaObservation(
            org_id=ticket.org_id,
            branch_id=ticket.agency_id,
            service_id=ticket.service_id,
            counter_id=counter_id,
            ticket_id=ticket.id,
            event_key=event_key,
            duration_seconds=duration,
            eligible=False,
            exclusion_reason=f"interrupted_{reason}",
            observed_at=ended_at,
            service_started_at=started_at,
            service_completed_at=ended_at,
        ))
        await self.db.flush()
        return True

    async def _is_outlier(self, service: Service, duration: int) -> bool:
        rows = (await self.db.execute(
            select(EtaObservation.duration_seconds)
            .where(
                EtaObservation.org_id == service.org_id,
                EtaObservation.service_id == service.id,
                EtaObservation.eligible.is_(True),
                EtaObservation.duration_seconds.is_not(None),
            )
            .order_by(EtaObservation.observed_at.desc())
            .limit(100)
        )).scalars().all()
        values = [int(value) for value in rows]
        if len(values) < service.eta_outlier_min_samples:
            return False
        center = statistics.median(values)
        mad = statistics.median([abs(value - center) for value in values])
        if mad == 0:
            # With a degenerate distribution MAD cannot distinguish a genuine
            # new service pattern from an outlier; retain the raw observation.
            return False
        spread = service.eta_outlier_mad_multiplier * 1.4826 * mad
        return duration < max(0, center - spread) or duration > center + spread

    async def record_prediction_at_call(self, ticket: Ticket, called_at: datetime) -> None:
        """Evaluate the latest delivered remaining-wait forecast at first call."""
        if not ticket.org_id:
            return
        prediction = (await self.db.execute(
            select(EtaPrediction)
            .where(
                EtaPrediction.ticket_id == ticket.id,
                EtaPrediction.org_id == ticket.org_id,
                EtaPrediction.branch_id == ticket.agency_id,
                EtaPrediction.service_id == ticket.service_id,
            )
            .order_by(EtaPrediction.version.desc())
            .limit(1)
        )).scalar_one_or_none()
        if (
            prediction is None
            or prediction.evaluated_at
        ):
            return
        prediction.counter_id = ticket.counter_id
        actual_seconds = max(0, int((_as_utc(called_at) - _as_utc(prediction.calculated_at)).total_seconds()))
        prediction.actual_wait_seconds = actual_seconds
        prediction.absolute_error_seconds = (
            abs(prediction.estimated_wait_seconds - actual_seconds)
            if prediction.estimated_wait_seconds is not None else None
        )
        prediction.evaluated_at = _as_utc(called_at)

    async def _persist_forecasts(
        self, service: Service, waiting: list[Ticket], estimate: ServiceQueueEstimate
    ) -> None:
        if not waiting or not service.org_id or estimate.calculated_at is None:
            return
        ticket_ids = [ticket.id for ticket in waiting]
        # Serialize version assignment for concurrent snapshots of one service.
        await self.db.scalar(
            select(Service.id)
            .where(Service.id == service.id, Service.org_id == service.org_id)
            .with_for_update()
        )
        latest_versions = (
            select(EtaPrediction.ticket_id.label("ticket_id"), func.max(EtaPrediction.version).label("version"))
            .where(
                EtaPrediction.org_id == service.org_id,
                EtaPrediction.service_id == service.id,
                EtaPrediction.ticket_id.in_(ticket_ids),
            )
            .group_by(EtaPrediction.ticket_id)
            .subquery()
        )
        existing = (await self.db.execute(
            select(EtaPrediction).join(
                latest_versions,
                and_(
                    EtaPrediction.ticket_id == latest_versions.c.ticket_id,
                    EtaPrediction.version == latest_versions.c.version,
                ),
            )
        )).scalars().all()
        by_ticket = {prediction.ticket_id: prediction for prediction in existing}
        for ticket in waiting:
            value = estimate.ticket_details.get(ticket.id, EtaValue(None, "unavailable", "none", 0))
            prediction = by_ticket.get(ticket.id)
            if prediction is not None:
                same_minute = (
                    (prediction.estimated_wait_seconds is None and value.wait_seconds is None)
                    or (
                        prediction.estimated_wait_seconds is not None
                        and value.wait_seconds is not None
                        and _ceil_minutes(prediction.estimated_wait_seconds) == _ceil_minutes(value.wait_seconds)
                    )
                )
                recent = (estimate.calculated_at - _as_utc(prediction.calculated_at)).total_seconds() < 60
                if (
                    prediction.evaluated_at is None
                    and same_minute
                    and recent
                    and prediction.confidence == value.confidence
                    and prediction.profile_level == value.profile_level
                ):
                    continue
            ticket.estimated_wait_minutes = (
                _ceil_minutes(value.wait_seconds) if value.wait_seconds is not None else None
            )
            self.db.add(EtaPrediction(
                ticket_id=ticket.id,
                org_id=service.org_id,
                branch_id=service.branch_id,
                service_id=service.id,
                estimated_wait_seconds=value.wait_seconds,
                profile_level=value.profile_level,
                confidence=value.confidence,
                sample_count=value.sample_count,
                calculated_at=estimate.calculated_at,
                version=(prediction.version + 1) if prediction is not None else 1,
            ))

    async def metrics(
        self, *, org_id: str, service_id: str, since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict:
        latest = (
            select(
                EtaPrediction.ticket_id.label("ticket_id"),
                EtaPrediction.counter_id.label("counter_id"),
                EtaPrediction.absolute_error_seconds.label("absolute_error_seconds"),
                EtaPrediction.evaluated_at.label("evaluated_at"),
                func.row_number().over(
                    partition_by=EtaPrediction.ticket_id,
                    order_by=EtaPrediction.version.desc(),
                ).label("rn"),
            )
            .where(
                EtaPrediction.org_id == org_id,
                EtaPrediction.service_id == service_id,
            )
            .subquery()
        )
        latest_filters = [latest.c.rn == 1, latest.c.absolute_error_seconds.is_not(None)]
        if since:
            latest_filters.append(latest.c.evaluated_at >= _as_utc(since))
        if until:
            latest_filters.append(latest.c.evaluated_at < _as_utc(until))
        count, mean_error = (await self.db.execute(
            select(func.count(latest.c.ticket_id), func.avg(latest.c.absolute_error_seconds))
            .where(*latest_filters)
        )).one()
        counter_rows = (await self.db.execute(
            select(
                latest.c.counter_id,
                func.count(latest.c.ticket_id),
                func.avg(latest.c.absolute_error_seconds),
            )
            .where(*latest_filters)
            .group_by(latest.c.counter_id)
            .order_by(latest.c.counter_id)
        )).all()
        return {
            "org_id": org_id,
            "service_id": service_id,
            "evaluated_forecasts": int(count or 0),
            "mae_seconds": float(mean_error) if mean_error is not None else None,
            "mae_minutes": (float(mean_error) / 60) if mean_error is not None else None,
            "by_counter": [
                {
                    "counter_id": counter_id,
                    "evaluated_forecasts": int(counter_count or 0),
                    "mae_seconds": float(counter_error) if counter_error is not None else None,
                    "mae_minutes": float(counter_error) / 60 if counter_error is not None else None,
                }
                for counter_id, counter_count, counter_error in counter_rows
            ],
            "since": since,
            "until": until,
        }


def ewma_step(previous_seconds: float, observation_seconds: int, alpha: float) -> float:
    return alpha * observation_seconds + (1 - alpha) * previous_seconds


def _unavailable(calculated_at: datetime, version: int = 0) -> ServiceQueueEstimate:
    return ServiceQueueEstimate({}, None, {}, EtaValue(None, "unavailable", "none", 0), calculated_at, version)


def _confidence(level: str, samples: int, penalty: bool, min_service: int, min_counter: int) -> str:
    if level == "counter" and samples >= min_counter and not penalty:
        return "high"
    if level in {"counter", "service"} and samples >= min_service and not penalty:
        return "medium"
    return "low"


def _ceil_minutes(seconds: int | None) -> int:
    return max(0, math.ceil(max(0, seconds or 0) / 60))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
