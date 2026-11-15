"""
Tests — rapport quotidien d'agence (phase 4).

Deux propriétés sont éprouvées ici, et une seule des deux est technique :

1. **La journée est locale.** Un rapport daté du 14 novembre doit couvrir
   22:00 UTC le 13 → 22:00 UTC le 14 (UTC+2). En UTC, la journée commencerait à
   02:00 heure locale et couperait la matinée en deux — le gérant verrait un
   rapport « du 14 » amputé de sa première heure de travail, et une heure de
   pointe décalée de deux heures.

2. **Les chiffres sont ceux de `queue_logs`**, pas un compteur intermédiaire.
   Un rapport qui se trompe est pire qu'aucun rapport : il fait prendre des
   décisions de staffing sur des données fausses.

Les lignes `queue_logs` sont insérées avec des `created_at` naïfs représentant
UTC : c'est ainsi que le dialecte SQLite les stocke (sans offset), et cela rend
les comparaisons de bornes déterministes.
"""
from argparse import Namespace
from datetime import date, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import settings
from app.models.branch import Branch, Service
from app.models.organization import Organization
from app.models.queue_log import QueueLog, QueueLogAction
from app.services import report_service
from app.services.bootstrap_service import BootstrapError
from app.services.report_service import (
    DailyReportService,
    deliver_reports,
    french_date,
    local_day_bounds,
    render_daily_report,
    report_subject,
)

DAY = date(2026, 11, 14)


# ── Jeu de données ──────────────────────────────────────────────────────────

async def _org_branch(db_session, *, org_slug="ulbu", branch_slug="ulbu-mutanga-nord",
                      email="mutanga@ulbu.bi", active=True):
    org = Organization(name="ULBU", slug=org_slug, sector="university")
    db_session.add(org)
    await db_session.flush()

    branch = Branch(
        org_id=org.id,
        name="ULBU Mutanga-Nord",
        slug=branch_slug,
        sector="university",
        city="Bujumbura",
        email=email,
        is_active=active,
    )
    db_session.add(branch)
    await db_session.flush()

    service = Service(
        org_id=org.id, branch_id=branch.id, name="Caisse", ticket_prefix="A"
    )
    db_session.add(service)
    await db_session.commit()
    await db_session.refresh(branch)
    await db_session.refresh(service)
    return org, branch, service


async def _log(db_session, org, branch, service, action, at, **kwargs):
    entry = QueueLog(
        org_id=org.id,
        ticket_id=kwargs.pop("ticket_id", "t-" + action.value + "-" + at.isoformat()),
        agency_id=branch.id,
        service_id=service.id,
        action=action,
        to_status=kwargs.pop("to_status", "waiting"),
        created_at=at,
        **kwargs,
    )
    db_session.add(entry)
    await db_session.commit()
    return entry


async def _sample_day(db_session, org, branch, service):
    """
    Une journée type : 3 tickets créés, 2 appelés (10 et 20 min d'attente),
    1 absent, 2 servis, 1 annulé.

    Le pic est à 08:00 UTC = 10:00 locales : c'est ce que le rapport doit
    afficher.
    """
    await _log(db_session, org, branch, service, QueueLogAction.TICKET_CREATED,
               datetime(2026, 11, 14, 8, 0))
    await _log(db_session, org, branch, service, QueueLogAction.TICKET_CREATED,
               datetime(2026, 11, 14, 8, 5))
    await _log(db_session, org, branch, service, QueueLogAction.TICKET_CREATED,
               datetime(2026, 11, 14, 12, 0))

    await _log(db_session, org, branch, service, QueueLogAction.TICKET_CALLED,
               datetime(2026, 11, 14, 8, 10), elapsed_seconds=600)
    await _log(db_session, org, branch, service, QueueLogAction.TICKET_CALLED,
               datetime(2026, 11, 14, 8, 30), elapsed_seconds=1200)
    await _log(db_session, org, branch, service, QueueLogAction.TICKET_ABSENT,
               datetime(2026, 11, 14, 9, 0))
    await _log(db_session, org, branch, service, QueueLogAction.TICKET_DONE,
               datetime(2026, 11, 14, 8, 20), counter_name="Guichet 1",
               counter_id="c-1")
    await _log(db_session, org, branch, service, QueueLogAction.TICKET_DONE,
               datetime(2026, 11, 14, 8, 40), counter_name="Guichet 1",
               counter_id="c-1")
    await _log(db_session, org, branch, service, QueueLogAction.TICKET_CANCELLED,
               datetime(2026, 11, 14, 11, 0))


# ── 1. Bornes de la journée locale ──────────────────────────────────────────

def test_journee_locale_decalee_en_utc():
    """14 novembre à Bujumbura = 13/11 22:00 UTC → 14/11 22:00 UTC."""
    start, end = local_day_bounds(DAY, 2)

    assert start == datetime(2026, 11, 13, 22, 0, tzinfo=start.tzinfo)
    assert end == datetime(2026, 11, 14, 22, 0, tzinfo=end.tzinfo)
    assert (end - start).total_seconds() == 86_400


def test_journee_utc_sans_decalage():
    start, end = local_day_bounds(DAY, 0)

    assert start.hour == 0 and start.day == 14
    assert end.hour == 0 and end.day == 15


# ── 2. Chiffres du rapport ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chiffres_de_la_journee(db_session):
    org, branch, service = await _org_branch(db_session)
    await _sample_day(db_session, org, branch, service)

    reports = await DailyReportService(db_session).build_reports(day=DAY)
    assert len(reports) == 1

    totals = reports[0]["totals"]
    assert totals["created"] == 3
    assert totals["done"] == 2
    assert totals["cancelled"] == 1
    assert totals["called"] == 2
    assert totals["absent"] == 1
    assert totals["avg_wait_minutes"] == 15.0
    assert totals["absence_rate_percent"] == 50.0


@pytest.mark.asyncio
async def test_heure_de_pointe_en_heure_locale(db_session):
    """
    08:00 UTC = 10:00 locales.

    Annoncer « 8h » au gérant serait faux de deux heures — et l'enverrait
    renforcer un guichet au mauvais moment.
    """
    org, branch, service = await _org_branch(db_session)
    await _sample_day(db_session, org, branch, service)

    reports = await DailyReportService(db_session).build_reports(day=DAY)

    assert reports[0]["totals"]["busiest_hour_local"] == 10


@pytest.mark.asyncio
async def test_services_et_guichets(db_session):
    org, branch, service = await _org_branch(db_session)
    await _sample_day(db_session, org, branch, service)

    reports = await DailyReportService(db_session).build_reports(day=DAY)

    assert reports[0]["services"][0]["service_name"] == "Caisse"
    assert reports[0]["services"][0]["total_tickets"] == 3
    assert reports[0]["counters"][0]["counter_name"] == "Guichet 1"
    assert reports[0]["counters"][0]["tickets_done"] == 2


@pytest.mark.asyncio
async def test_tickets_de_la_veille_au_soir_exclus(db_session):
    """
    Un ticket créé le 13 à 20:00 UTC (22:00 locales le 13) appartient au 13.

    C'est le piège du rapport en UTC : cette ligne serait comptée dans le 14
    alors qu'elle relève de la journée précédente.
    """
    org, branch, service = await _org_branch(db_session)
    await _log(db_session, org, branch, service, QueueLogAction.TICKET_CREATED,
               datetime(2026, 11, 13, 20, 0))

    reports = await DailyReportService(db_session).build_reports(day=DAY)

    assert reports[0]["totals"]["created"] == 0
    assert reports[0]["has_activity"] is False


@pytest.mark.asyncio
async def test_ticket_de_la_nuit_suivante_inclus(db_session):
    """
    14 à 21:30 UTC = 23:30 locales le 14 → encore dans la journée du 14.

    En UTC, cette ligne tomberait dans le 14 aussi — mais le 15 à 00:30 locales
    (= 22:30 UTC le 14) doit, lui, basculer sur le 15.
    """
    org, branch, service = await _org_branch(db_session)
    await _log(db_session, org, branch, service, QueueLogAction.TICKET_CREATED,
               datetime(2026, 11, 14, 21, 30))
    await _log(db_session, org, branch, service, QueueLogAction.TICKET_CREATED,
               datetime(2026, 11, 14, 22, 30))

    reports = await DailyReportService(db_session).build_reports(day=DAY)

    assert reports[0]["totals"]["created"] == 1


@pytest.mark.asyncio
async def test_isolation_multi_tenant(db_session):
    """Les tickets d'une autre organisation ne doivent jamais apparaître."""
    org, branch, service = await _org_branch(db_session)
    other_org, other_branch, other_service = await _org_branch(
        db_session, org_slug="autre-banque", branch_slug="autre-agence",
        email="agence@autre.bi",
    )
    await _log(db_session, org, branch, service, QueueLogAction.TICKET_CREATED,
               datetime(2026, 11, 14, 9, 0))
    await _log(db_session, other_org, other_branch, other_service,
               QueueLogAction.TICKET_CREATED, datetime(2026, 11, 14, 9, 0))

    reports = await DailyReportService(db_session).build_reports(
        day=DAY, branch_slug="ulbu-mutanga-nord"
    )

    assert len(reports) == 1
    assert reports[0]["totals"]["created"] == 1
    assert reports[0]["branch"]["name"] == "ULBU Mutanga-Nord"


@pytest.mark.asyncio
async def test_agence_desactivee_ignoree(db_session):
    await _org_branch(db_session, branch_slug="fermee", email="x@ulbu.bi", active=False)

    reports = await DailyReportService(db_session).build_reports(day=DAY)

    assert reports == []


@pytest.mark.asyncio
async def test_journee_sans_activite_reste_valide(db_session):
    """Une agence qui n'a rien reçu doit produire un rapport, pas une erreur."""
    await _org_branch(db_session)

    reports = await DailyReportService(db_session).build_reports(day=DAY)

    assert len(reports) == 1
    assert reports[0]["has_activity"] is False
    assert reports[0]["totals"]["created"] == 0


# ── 3. Rendu texte ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rendu_contient_les_chiffres(db_session):
    org, branch, service = await _org_branch(db_session)
    await _sample_day(db_session, org, branch, service)

    report = (await DailyReportService(db_session).build_reports(day=DAY))[0]
    text = render_daily_report(report)

    assert "ULBU Mutanga-Nord" in text
    assert "samedi 14 novembre 2026" in text
    assert "Tickets créés" in text and "3" in text
    assert "15,0 min" in text
    assert "50,0 %" in text
    assert "10h00 (heure locale)" in text
    assert "Caisse" in text
    assert "Guichet 1" in text


@pytest.mark.asyncio
async def test_rendu_journee_vide(db_session):
    """Le rapport vide dit explicitement qu'il n'y a rien — pas une page blanche."""
    await _org_branch(db_session)
    report = (await DailyReportService(db_session).build_reports(day=DAY))[0]

    text = render_daily_report(report)

    assert "Aucune activité enregistrée ce jour." in text
    assert "Par service" not in text


def test_objet_du_message():
    report = {
        "date_label": "samedi 14 novembre 2026",
        "branch": {"name": "ULBU Mutanga-Nord"},
    }
    assert report_subject(report) == "TONDE — Rapport du samedi 14 novembre 2026 — ULBU Mutanga-Nord"


def test_date_francaise_premier_du_mois():
    """« 1er », pas « 1 » : c'est la seule irrégularité du français écrit."""
    assert french_date(date(2026, 12, 1)) == "mardi 1er décembre 2026"


def test_nombres_au_format_francais():
    assert report_service._number(1250) == "1\u202f250"
    assert report_service._number(12.44, 1) == "12,4"
    assert report_service._number(0) == "0"


# ── 4. Envoi ────────────────────────────────────────────────────────────────

def _report(**overrides):
    report = {
        "date": "2026-11-14",
        "date_label": "samedi 14 novembre 2026",
        "branch": {"name": "ULBU Mutanga-Nord", "email": "mutanga@ulbu.bi"},
        "totals": {
            "created": 0, "done": 0, "cancelled": 0, "incomplete": 0,
            "transferred": 0, "called": 0, "absent": 0,
            "avg_wait_minutes": 0.0, "absence_rate_percent": 0.0,
            "busiest_hour_local": None,
        },
        "services": [],
        "counters": [],
        "has_activity": False,
    }
    report.update(overrides)
    return report


@pytest.mark.asyncio
async def test_simulation_n_envoie_rien():
    with patch.object(report_service, "send_email", new_callable=AsyncMock) as sender:
        results = await deliver_reports([_report()], dry_run=True)

    sender.assert_not_awaited()
    assert results[0]["reason"] == "dry_run"


@pytest.mark.asyncio
async def test_envoi_au_email_de_l_agence(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")

    with patch.object(report_service, "send_email", new_callable=AsyncMock) as sender:
        results = await deliver_reports([_report()])

    sender.assert_awaited_once()
    assert sender.await_args.args[0] == "mutanga@ulbu.bi"
    assert results[0]["sent"] is True
    # En développement l'email n'est que journalisé : le résultat doit le dire.
    assert results[0]["simulated"] is True


@pytest.mark.asyncio
async def test_destinataire_impose_prime():
    with patch.object(report_service, "send_email", new_callable=AsyncMock) as sender:
        await deliver_reports([_report()], to="directeur@ulbu.bi")

    assert sender.await_args.args[0] == "directeur@ulbu.bi"


@pytest.mark.asyncio
async def test_agence_sans_email_non_envoyee():
    with patch.object(report_service, "send_email", new_callable=AsyncMock) as sender:
        results = await deliver_reports([_report(branch={"name": "Sans email", "email": None})])

    sender.assert_not_awaited()
    assert results[0]["reason"] == "no_recipient"


@pytest.mark.asyncio
async def test_smtp_non_configure_en_production(monkeypatch):
    """
    En production sans SMTP, l'envoi est refusé avec une raison explicite.

    Le cron doit pouvoir distinguer « rien à envoyer » de « mal configuré » :
    sans cette distinction, le rapport disparaît silencieusement.
    """
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "SMTP_HOST", "")

    with patch.object(report_service, "send_email", new_callable=AsyncMock) as sender:
        results = await deliver_reports([_report()])

    sender.assert_not_awaited()
    assert results[0]["reason"] == "smtp_not_configured"


@pytest.mark.asyncio
async def test_echec_smtp_remonte(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")

    with patch.object(
        report_service, "send_email", new_callable=AsyncMock,
        side_effect=report_service.EmailDeliveryError("timeout"),
    ):
        results = await deliver_reports([_report()])

    assert results[0]["sent"] is False
    assert results[0]["reason"] == "smtp_error"
    assert "timeout" in results[0]["error"]


# ── 5. Commande CLI ─────────────────────────────────────────────────────────

def test_cli_accepte_les_options():
    from app.cli import _build_parser

    args = _build_parser().parse_args(
        ["daily-report", "--date", "2026-11-14", "--branch", "ulbu-mutanga-nord", "--send"]
    )

    assert args.command == "daily-report"
    assert args.date == "2026-11-14"
    assert args.branch == "ulbu-mutanga-nord"
    assert args.send is True


def test_cli_sans_envoi_par_defaut():
    """Sans `--send`, la commande affiche seulement : l'envoi reste un acte explicite."""
    from app.cli import _build_parser

    args = _build_parser().parse_args(["daily-report"])

    assert args.send is False
    assert args.date is None  # → hier


@pytest.mark.asyncio
async def test_cli_date_invalide_refusee():
    """Une date mal formée doit être dite, pas silencieusement remplacée par hier."""
    from app.cli import _run_daily_report

    args = Namespace(date="14/11/2026", org=None, branch=None, to=None, send=False, json=False)

    with pytest.raises(BootstrapError) as exc_info:
        await _run_daily_report(args)

    assert "AAAA-MM-JJ" in str(exc_info.value)
