"""Historical ticket creation backfill. Never changes tickets, queue logs or Redis.

Only a unique TICKET_CREATED QueueLog proves the original service. A corroborated
first transfer can support a documented inference; incomplete/contradictory chains
are excluded. A dry run issues SELECT statements only.
"""
import hashlib
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.branch import Service
from app.models.qape import AbuseEvent
from app.models.queue_log import QueueLog, QueueLogAction
from app.models.ticket import Ticket

logger = logging.getLogger(__name__)


@dataclass
class BackfillReport:
    dry_run: bool
    as_of: str
    after_id: str | None
    last_id: str | None = None
    examined: int = 0
    recoverable: int = 0
    certain: int = 0
    inferred_count: int = 0
    inserted: int = 0
    already_recorded: int = 0
    ambiguous: list[dict] = field(default_factory=list)
    inferred: list[dict] = field(default_factory=list)
    batches: int = 0
    complete_scan: bool = False
    reconciliation_required: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def _as_utc(value: datetime) -> datetime:
    # Historical SQLite tests store naive UTC; production timestamptz is aware.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _origin(ticket: Ticket, logs: list[QueueLog], services: dict[str, Service]) -> tuple[str | None, str, str]:
    created = [log for log in logs if log.action == QueueLogAction.TICKET_CREATED]
    transfers = [log for log in logs if log.action == QueueLogAction.TICKET_TRANSFERRED]
    if len(created) > 1:
        return None, "ambiguous", "multiple_creation_logs"
    if created:
        source = created[0].service_id
        if (created[0].org_id != ticket.org_id or
                _as_utc(created[0].created_at) < _as_utc(ticket.created_at) or
                (transfers and _as_utc(created[0].created_at) > _as_utc(transfers[0].created_at))):
            return None, "ambiguous", "creation_log_inconsistent"
        quality = "certain"
        evidence = "queue_log_creation"
    elif transfers:
        first = transfers[0]
        try:
            extra = json.loads(first.extra_data or "{}")
        except (ValueError, TypeError):
            return None, "ambiguous", "invalid_transfer_evidence"
        source = first.service_id
        # Corroboration from two independent fields and an earlier log from the
        # same service. This is an INFERENCE, never described as certain.
        prior = [log for log in logs if log.created_at <= first.created_at and log.id != first.id]
        if (extra.get("from_service_id") != source or
                not any(log.service_id == source and log.org_id == ticket.org_id for log in prior)):
            return None, "ambiguous", "missing_creation_and_transfer_evidence"
        quality = "inferred"
        evidence = "first_transfer_and_prior_log"
    else:
        return None, "ambiguous", "missing_creation_log"
    svc = services.get(source)
    if svc is None or svc.org_id != ticket.org_id:
        return None, "ambiguous", "origin_service_missing_or_foreign"
    # Check durable transfer chain: source before each transfer, destination in
    # extra_data, and final destination equal to today's Ticket.service_id.
    current = source
    for log in transfers:
        try:
            extra = json.loads(log.extra_data or "{}")
        except (ValueError, TypeError):
            return None, "ambiguous", "invalid_transfer_evidence"
        target = extra.get("to_service_id")
        if (log.org_id != ticket.org_id or log.service_id != current or
                extra.get("from_service_id") != current or not isinstance(target, str) or
                target not in services or services[target].org_id != ticket.org_id):
            return None, "ambiguous", "broken_transfer_chain"
        current = target
    if current != ticket.service_id:
        return None, "ambiguous", "final_service_mismatch"
    return source, quality, evidence


async def run_backfill(db: AsyncSession, *, dry_run: bool = True, batch_size: int = 200,
                       after_id: str | None = None, as_of: datetime | None = None,
                       max_batches: int | None = None) -> BackfillReport:
    """Scan by ticket ID. Resume with BOTH last_id and unchanged as_of.

    Caller must provide a disposable database for tests. apply=True is the only
    writing mode; transactions commit once per batch. Report last_id is a safe
    checkpoint after a completed batch, including already-recorded rows.
    """
    if not 1 <= batch_size <= 2000 or (max_batches is not None and max_batches < 1):
        raise ValueError("Invalid batch limits")
    if as_of is None:
        if after_id is not None:
            raise ValueError("Resuming requires the original as_of timestamp")
        as_of = datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone aware")
    report = BackfillReport(dry_run=dry_run, as_of=as_of.isoformat(), after_id=after_id)
    cursor = after_id
    while True:
        query = select(Ticket).where(Ticket.created_at <= as_of)
        if cursor:
            query = query.where(Ticket.id > cursor)
        tickets = list((await db.scalars(query.order_by(Ticket.id).limit(batch_size))).all())
        if not tickets:
            report.complete_scan = True
            break
        ids = [ticket.id for ticket in tickets]
        logs = (await db.scalars(select(QueueLog).where(QueueLog.ticket_id.in_(ids))
                                 .order_by(QueueLog.created_at, QueueLog.id))).all()
        grouped: dict[str, list[QueueLog]] = {tid: [] for tid in ids}
        for log in logs:
            grouped[log.ticket_id].append(log)
        origins = {log.service_id for log in logs} | {t.service_id for t in tickets}
        services = {svc.id: svc for svc in (await db.scalars(
            select(Service).where(Service.id.in_(origins)))).all()}
        existing = set((await db.scalars(select(AbuseEvent.ticket_id).where(
            AbuseEvent.ticket_id.in_(ids), AbuseEvent.event_type == "TICKET_CREATED"))).all())
        try:
            for ticket in tickets:
                report.examined += 1
                if ticket.id in existing:
                    report.already_recorded += 1
                    continue
                service_id, quality, evidence = _origin(ticket, grouped[ticket.id], services)
                if service_id is None:
                    report.ambiguous.append({"ticket_id": ticket.id, "reason": evidence})
                    continue
                report.recoverable += 1
                if quality == "inferred":
                    report.inferred_count += 1
                    report.inferred.append({"ticket_id": ticket.id, "evidence": evidence})
                else:
                    report.certain += 1
                if dry_run:
                    continue
                # Savepoint handles a concurrent worker's unique-index conflict.
                # Any other integrity failure aborts the entire batch visibly.
                try:
                    async with db.begin_nested():
                        db.add(AbuseEvent(
                            id=str(uuid.uuid4()), org_id=ticket.org_id, user_id=ticket.user_id,
                            service_id=service_id, ticket_id=ticket.id,
                            request_id=hashlib.sha256(("qape-backfill:v1:" + ticket.id).encode()).hexdigest(),
                            event_type="TICKET_CREATED",
                            provenance="HISTORICAL_CERTAIN" if quality == "certain" else "HISTORICAL_INFERRED",
                            decision=None, reason_codes=["HISTORICAL_CREATION"],
                            policy_version=None, created_at=ticket.created_at,
                            details={},
                        ))
                        await db.flush()
                except IntegrityError:
                    # Distinguish the intended idempotent conflict from broken data.
                    found = await db.scalar(select(AbuseEvent.id).where(
                        AbuseEvent.ticket_id == ticket.id, AbuseEvent.event_type == "TICKET_CREATED"))
                    if found is None:
                        raise
                    report.already_recorded += 1
                    report.recoverable -= 1
                    if quality == "inferred":
                        report.inferred_count -= 1
                        report.inferred.pop()
                    else:
                        report.certain -= 1
                    continue
                report.inserted += 1
            if not dry_run:
                await db.commit()
            else:
                # SELECT-only dry-run; close any implicit read transaction.
                await db.rollback()
        except Exception:
            await db.rollback()
            raise
        cursor = ids[-1]
        report.last_id = cursor
        report.batches += 1
        logger.info("QAPE backfill batch: %s", {"last_id": cursor, "examined": report.examined,
                    "inserted": report.inserted, "ambiguous": len(report.ambiguous)})
        if max_batches is not None and report.batches >= max_batches:
            break
    # A resumed keyset scan can miss a late backdated INSERT whose ID precedes
    # the saved cursor. Only a NEW full-range dry-run with a fresh as_of can
    # certify that no recoverable ticket remains at that snapshot. The operator
    # must quiesce historical writes for the final reconciliation cutover.
    report.reconciliation_required = not (
        dry_run and after_id is None and max_batches is None
        and report.complete_scan and report.recoverable == 0
    )
    return report
