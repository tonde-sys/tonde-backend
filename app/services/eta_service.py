"""Estimation dynamique des temps d'attente par simulation des guichets.

Le calcul reste déterministe et sans effet de bord sur la file : il lit les
attentes réelles, estime la durée des services et simule leur affectation aux
guichets ouverts. Les historiques individuels sont lissés vers la moyenne du
service pour éviter qu'un petit nombre de dossiers atypiques ne domine l'ETA.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.branch import Service
from app.models.counter import Counter
from app.models.employee import Employee, EmployeeStatus
from app.models.queue_log import QueueLog, QueueLogAction
from app.models.ticket import PRIORITY_SCORES, Ticket, TicketStatus
from app.models.user import UserRole

ETA_SAMPLE_SIZE = 10
ETA_HISTORY_LIMIT = 100
ETA_HISTORY_DAYS = 90
ETA_PROFILE_PRIOR_WEIGHT = 4
ETA_MIN_SERVICE_SECONDS = 30
ETA_MAX_SERVICE_SECONDS = 3600
ETA_CALLED_HANDOFF_SECONDS = 30


@dataclass(frozen=True)
class ServiceQueueEstimate:
    """ETA de chaque ticket en attente et d'un prochain arrivant."""

    ticket_minutes: dict[str, int]
    next_ticket_minutes: int


@dataclass
class _Lane:
    counter_id: str | None
    agent_id: str | None
    free_in_seconds: int = 0


@dataclass
class _ServiceProfile:
    baseline_seconds: int
    counter_seconds: dict[str, int] = field(default_factory=dict)
    agent_seconds: dict[str, int] = field(default_factory=dict)
    counter_samples: dict[str, list[int]] = field(default_factory=dict)
    agent_samples: dict[str, list[int]] = field(default_factory=dict)

    def duration_for(self, counter_id: str | None, agent_id: str | None) -> int:
        if agent_id and agent_id in self.agent_seconds:
            return self.agent_seconds[agent_id]
        if counter_id and counter_id in self.counter_seconds:
            return self.counter_seconds[counter_id]
        return self.baseline_seconds

    def remaining_for(
        self,
        counter_id: str | None,
        agent_id: str | None,
        elapsed_seconds: int,
    ) -> int:
        """Prévoit le temps résiduel depuis la distribution observée."""
        expected = self.duration_for(counter_id, agent_id)
        if agent_id:
            samples = self.agent_samples.get(agent_id, [])
        else:
            samples = []
        if not samples and counter_id:
            samples = self.counter_samples.get(counter_id, [])
        if not samples:
            return max(0, expected - elapsed_seconds)

        elapsed = max(0, elapsed_seconds)
        residuals = [max(0, sample - elapsed) for sample in samples if sample >= elapsed]
        if residuals:
            return max(0, int(round(sum(residuals) / len(residuals))))
        # Si le ticket en cours dépasse tous les exemples comparables, il s'agit
        # probablement d'un dossier atypique. Garder un petit résiduel évite de
        # prédire qu'un guichet occupé se libère instantanément.
        return min(expected, max(30, int(expected * 0.1)))


class EtaEstimator:
    """Construit une prévision cohérente pour un ou plusieurs services."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def estimate_many(
        self,
        services: list[Service],
        *,
        now: datetime | None = None,
    ) -> dict[str, ServiceQueueEstimate]:
        """Simule les files des services fournis en partageant leur capacité de branche.

        Tous les guichets actifs et ouverts d'une branche sont considérés aptes à
        appeler les services de cette branche, conformément à l'API actuelle de
        `call_next`. Les tickets déjà appelés ou en service chargent leur guichet.
        """
        targets = {service.id: service for service in services}
        if not targets:
            return {}
        now_utc = _as_utc(now or datetime.now(timezone.utc))
        branch_ids = {service.branch_id for service in services}

        # Charger les services de la branche aussi : un guichet occupé peut traiter
        # un autre service, et sa charge en cours doit compter dans la prévision.
        catalog_rows = (await self.db.execute(
            select(Service).where(Service.branch_id.in_(branch_ids))
        )).scalars().all()
        catalog = {service.id: service for service in catalog_rows}
        catalog.update(targets)

        waiting_rows = (await self.db.execute(
            select(Ticket).where(
                Ticket.service_id.in_(targets),
                Ticket.agency_id.in_(branch_ids),
                Ticket.status == TicketStatus.WAITING,
            )
        )).scalars().all()
        waiting_by_service: dict[str, list[Ticket]] = {service_id: [] for service_id in targets}
        for ticket in waiting_rows:
            if ticket.service_id in waiting_by_service:
                waiting_by_service[ticket.service_id].append(ticket)
        for queue in waiting_by_service.values():
            queue.sort(key=lambda ticket: (
                -PRIORITY_SCORES.get(ticket.priority, 0),
                ticket.queue_sequence,
                ticket.created_at,
                ticket.id,
            ))

        counters = (await self.db.execute(
            select(Counter)
            .where(
                Counter.agency_id.in_(branch_ids),
                Counter.is_active.is_(True),
                Counter.is_open.is_(True),
            )
            .order_by(Counter.name, Counter.id)
        )).scalars().all()
        counters_by_branch: dict[str, list[Counter]] = {branch_id: [] for branch_id in branch_ids}
        for counter in counters:
            counters_by_branch.setdefault(counter.agency_id, []).append(counter)

        current_ids = list({counter.current_ticket_id for counter in counters if counter.current_ticket_id})
        current_rows = []
        if current_ids:
            current_rows = (await self.db.execute(
                select(Ticket).where(
                    Ticket.id.in_(current_ids),
                    Ticket.status.in_([TicketStatus.CALLED, TicketStatus.SERVING]),
                )
            )).scalars().all()
        current_by_id = {ticket.id: ticket for ticket in current_rows}

        active_agents_by_counter: dict[str, list[str]] = {}
        counter_ids = [counter.id for counter in counters]
        if counter_ids:
            employee_rows = (await self.db.execute(
                select(Employee.counter_id, Employee.user_id)
                .where(
                    Employee.counter_id.in_(counter_ids),
                    Employee.status == EmployeeStatus.ACTIVE,
                    Employee.role == UserRole.AGENT,
                )
                .order_by(Employee.updated_at.desc(), Employee.created_at.desc())
            )).all()
            for counter_id, user_id in employee_rows:
                if counter_id and user_id:
                    active_agents_by_counter.setdefault(counter_id, []).append(user_id)

        # N'utiliser le rythme individuel que si un seul agent actif est affecté
        # au guichet. Plusieurs agents affectés ne disent pas qui travaille ici.
        assigned_agents = {
            counter_id: users[0]
            for counter_id, users in active_agents_by_counter.items()
            if len(users) == 1
        }

        profiles = await self._load_profiles(catalog, now_utc)

        serving_agents: dict[str, str] = {}
        if current_ids:
            serving_logs = (await self.db.execute(
                select(QueueLog.ticket_id, QueueLog.actor_id)
                .where(
                    QueueLog.ticket_id.in_(current_ids),
                    QueueLog.action == QueueLogAction.TICKET_SERVING,
                    QueueLog.actor_id.is_not(None),
                )
                .order_by(QueueLog.created_at.desc())
            )).all()
            for ticket_id, actor_id in serving_logs:
                serving_agents.setdefault(ticket_id, actor_id)

        estimates: dict[str, ServiceQueueEstimate] = {}
        for service_id, target in targets.items():
            lanes: list[_Lane] = []
            for counter in counters_by_branch.get(target.branch_id, []):
                assigned_agent = assigned_agents.get(counter.id)
                current = current_by_id.get(counter.current_ticket_id or "")
                free_in = 0
                if current is not None:
                    current_profile = profiles.get(current.service_id)
                    if current_profile is None:
                        current_service = catalog.get(current.service_id)
                        fallback = current_service.avg_duration_minutes if current_service else 5
                        current_profile = _ServiceProfile(_bounded_seconds(fallback * 60))
                    active_agent = serving_agents.get(current.id) or assigned_agent
                    if current.status == TicketStatus.SERVING and current.served_at:
                        elapsed = int((now_utc - _as_utc(current.served_at)).total_seconds())
                        free_in = current_profile.remaining_for(
                            counter.id, active_agent, elapsed
                        )
                    else:
                        called_at = current.called_at or current.created_at
                        called_elapsed = max(
                            0, int((now_utc - _as_utc(called_at)).total_seconds())
                        )
                        handoff_left = max(
                            0,
                            min(
                                ETA_CALLED_HANDOFF_SECONDS,
                                settings.ABSENT_TIMEOUT_SECONDS - called_elapsed,
                            ),
                        )
                        free_in = handoff_left + current_profile.duration_for(
                            counter.id, active_agent
                        )
                lanes.append(_Lane(counter.id, assigned_agent, free_in))

            # Sans guichet ouvert, garder une voie virtuelle : l'ETA reste utile
            # et retombe sur le comportement séquentiel connu.
            if not lanes:
                lanes.append(_Lane(None, None, 0))

            profile = profiles.get(
                service_id,
                _ServiceProfile(_bounded_seconds(max(1, target.avg_duration_minutes) * 60)),
            )
            ticket_minutes: dict[str, int] = {}
            for ticket in waiting_by_service.get(service_id, []):
                lane = min(lanes, key=lambda item: (item.free_in_seconds, item.counter_id or ""))
                ticket_minutes[ticket.id] = _ceil_minutes(lane.free_in_seconds)
                lane.free_in_seconds += profile.duration_for(lane.counter_id, lane.agent_id)

            next_lane = min(lanes, key=lambda item: (item.free_in_seconds, item.counter_id or ""))
            estimates[service_id] = ServiceQueueEstimate(
                ticket_minutes=ticket_minutes,
                next_ticket_minutes=_ceil_minutes(next_lane.free_in_seconds),
            )
        return estimates

    async def _load_profiles(
        self,
        services: dict[str, Service],
        now: datetime,
    ) -> dict[str, _ServiceProfile]:
        if not services:
            return {}
        ranked = (
            select(
                Ticket.id.label("ticket_id"),
                Ticket.service_id.label("service_id"),
                Ticket.counter_id.label("counter_id"),
                Ticket.service_seconds.label("service_seconds"),
                func.row_number().over(
                    partition_by=Ticket.service_id,
                    order_by=(Ticket.done_at.desc(), Ticket.id.desc()),
                ).label("row_number"),
            )
            .where(
                Ticket.service_id.in_(services),
                Ticket.status == TicketStatus.DONE,
                Ticket.service_seconds.is_not(None),
                Ticket.service_seconds > 0,
                Ticket.done_at.is_not(None),
                Ticket.done_at >= now - timedelta(days=ETA_HISTORY_DAYS),
            )
            .subquery()
        )
        history = (await self.db.execute(
            select(
                ranked.c.ticket_id,
                ranked.c.service_id,
                ranked.c.counter_id,
                ranked.c.service_seconds,
            )
            .where(ranked.c.row_number <= ETA_HISTORY_LIMIT)
            .order_by(ranked.c.service_id, ranked.c.row_number)
        )).all()

        ticket_ids = [row.ticket_id for row in history]
        done_actors: dict[str, str] = {}
        if ticket_ids:
            actor_rows = (await self.db.execute(
                select(QueueLog.ticket_id, QueueLog.actor_id)
                .where(
                    QueueLog.ticket_id.in_(ticket_ids),
                    QueueLog.action == QueueLogAction.TICKET_DONE,
                    QueueLog.actor_id.is_not(None),
                )
                .order_by(QueueLog.created_at.desc())
            )).all()
            for ticket_id, actor_id in actor_rows:
                done_actors.setdefault(ticket_id, actor_id)

        durations_by_service: dict[str, list[int]] = {service_id: [] for service_id in services}
        durations_by_counter: dict[str, dict[str, list[int]]] = {
            service_id: {} for service_id in services
        }
        durations_by_agent: dict[str, dict[str, list[int]]] = {
            service_id: {} for service_id in services
        }
        durations_by_agent_counter: dict[str, dict[str, dict[str, list[int]]]] = {
            service_id: {} for service_id in services
        }
        for row in history:
            duration = _bounded_seconds(row.service_seconds)
            durations_by_service.setdefault(row.service_id, []).append(duration)
            if row.counter_id:
                durations_by_counter.setdefault(row.service_id, {}).setdefault(
                    row.counter_id, []
                ).append(duration)
            actor_id = done_actors.get(row.ticket_id)
            if actor_id:
                durations_by_agent.setdefault(row.service_id, {}).setdefault(
                    actor_id, []
                ).append(duration)
                if row.counter_id:
                    durations_by_agent_counter.setdefault(row.service_id, {}).setdefault(
                        actor_id, {}
                    ).setdefault(row.counter_id, []).append(duration)

        profiles: dict[str, _ServiceProfile] = {}
        for service_id, service in services.items():
            samples = durations_by_service.get(service_id, [])[:ETA_SAMPLE_SIZE]
            configured = _bounded_seconds(max(1, service.avg_duration_minutes) * 60)
            padding = max(0, ETA_SAMPLE_SIZE - len(samples))
            baseline = _bounded_seconds(
                (sum(samples) + configured * padding) / ETA_SAMPLE_SIZE
            )

            counter_seconds: dict[str, int] = {}
            counter_samples: dict[str, list[int]] = {}
            for counter_id, values in durations_by_counter.get(service_id, {}).items():
                recent = values[:ETA_SAMPLE_SIZE]
                counter_samples[counter_id] = recent
                counter_seconds[counter_id] = _shrink_mean(
                    recent, baseline, ETA_PROFILE_PRIOR_WEIGHT
                )

            agent_seconds: dict[str, int] = {}
            agent_samples: dict[str, list[int]] = {}
            for agent_id, values in durations_by_agent.get(service_id, {}).items():
                recent = values[:ETA_SAMPLE_SIZE]
                agent_samples[agent_id] = recent
                # Un agent est lissé vers son guichet habituel quand on peut
                # l'identifier ; sinon, vers la moyenne générale du service.
                counter_prior = baseline
                counter_groups = durations_by_agent_counter.get(service_id, {}).get(
                    agent_id, {}
                )
                if counter_groups:
                    agent_counter_samples = max(
                        counter_groups.values(), key=len
                    )[:ETA_SAMPLE_SIZE]
                    counter_prior = _shrink_mean(
                        agent_counter_samples,
                        baseline,
                        ETA_PROFILE_PRIOR_WEIGHT,
                    )
                agent_seconds[agent_id] = _shrink_mean(
                    recent, counter_prior, ETA_PROFILE_PRIOR_WEIGHT
                )

            profiles[service_id] = _ServiceProfile(
                baseline_seconds=baseline,
                counter_seconds=counter_seconds,
                agent_seconds=agent_seconds,
                counter_samples=counter_samples,
                agent_samples=agent_samples,
            )
        return profiles


def _shrink_mean(samples: list[int], prior: int, prior_weight: int) -> int:
    """Moyenne récente lissée vers un repère fiable lorsque n est faible."""
    if not samples:
        return _bounded_seconds(prior)
    estimate = (sum(samples) + prior * prior_weight) / (len(samples) + prior_weight)
    return _bounded_seconds(estimate)


def _bounded_seconds(seconds: float | int) -> int:
    return int(min(ETA_MAX_SERVICE_SECONDS, max(ETA_MIN_SERVICE_SECONDS, seconds)))


def _ceil_minutes(seconds: int) -> int:
    return max(0, math.ceil(max(0, seconds) / 60))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
