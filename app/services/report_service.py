"""
Rapport quotidien d'agence — dernière brique produit de la phase 4.

Le problème que ce module résout n'est pas technique : `queue_logs` contient
déjà tout ce qu'il faut pour savoir qu'une agence a servi 40 clients en 12
minutes d'attente moyenne hier. Personne ne le lit. Un gérant d'agence ne se
connecte pas à un tableau de bord chaque matin ; il lit ses emails. Le rapport
quotidien est donc le seul canal par lequel les données produites par TONDE
reviennent vers celui qui paie — sans lui, l'outil mesure sans rien changer.

Deux décisions de conception, toutes deux visibles dans la sortie :

1. **La journée est locale, pas UTC.** Un rapport daté du 14 novembre couvre
   22:00 UTC le 13 → 22:00 UTC le 14 (Burundi, UTC+2). En UTC, la « journée »
   commencerait à 02:00 heure locale et couperait la matinée en deux. L'heure
   de pointe est également reconvertie en heure locale.

2. **Le texte est le produit, pas un pis-aller.** Pas de HTML, pas de pièce
   jointe CSV : un email en texte brut, aligné, lisible sur un téléphone
   d'entrée de gamme avec une connexion à 200 kbit/s. C'est le terminal réel
   d'un gérant d'agence à Bujumbura.

L'envoi réutilise `email_gateway` — même passerelle que la réinitialisation de
mot de passe, même comportement en développement (journalisé, pas envoyé).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.branch import Branch
from app.models.organization import Organization
from app.services.analytics_service import AnalyticsService
from app.services.email_gateway import EmailDeliveryError, is_configured, send_email

logger = logging.getLogger(__name__)

# Noms français : la locale du serveur Render est C, `strftime("%A")` y répond
# « Saturday ». On ne veut pas dépendre d'un `locale.setlocale` global, qui
# affecte tout le processus (et casse le formatage des nombres ailleurs).
_WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
_MONTHS = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)


# ── Formatage ────────────────────────────────────────────────────────────────

def french_date(day: date) -> str:
    """« samedi 14 novembre 2026 » — « 1er » pour le premier du mois."""
    day_number = "1er" if day.day == 1 else str(day.day)
    return f"{_WEEKDAYS[day.weekday()]} {day_number} {_MONTHS[day.month - 1]} {day.year}"


def _number(value: float, decimals: int = 0) -> str:
    """
    Nombre au format français : virgule décimale, espace insécable fine pour
    les milliers. `12.4` → `12,4` ; `1250` → `1 250`.
    """
    if decimals:
        text = f"{value:.{decimals}f}"
    else:
        text = f"{round(value)}"
    integer, _, fraction = text.partition(".")
    sign = "-" if integer.startswith("-") else ""
    digits = integer.lstrip("-")
    groups = []
    while len(digits) > 3:
        groups.insert(0, digits[-3:])
        digits = digits[:-3]
    groups.insert(0, digits)
    grouped = "\u202f".join(groups)  # espace fine insécable
    result = f"{sign}{grouped}"
    return f"{result},{fraction}" if decimals else result


def _line(label: str, value: str, width: int = 24) -> str:
    return f"  {label:<{width}} {value}"


# ── Bornes de la journée locale ──────────────────────────────────────────────

def local_day_bounds(day: date, offset_hours: int) -> tuple[datetime, datetime]:
    """
    Convertit une date locale en intervalle UTC `[début, fin)`.

    Args:
        day: Journée visée, dans le fuseau de l'agence
        offset_hours: Décalage de l'agence par rapport à UTC (Burundi : +2)

    Returns:
        (début, fin) en UTC — la fin est exclue, pour ne pas compter deux fois
        un ticket créé exactement à minuit.
    """
    tz = timezone(timedelta(hours=offset_hours))
    start_local = datetime.combine(day, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _local_hour(utc_hour: int | None, offset_hours: int) -> int | None:
    """Heure UTC → heure locale (décalage entier, donc simple rotation)."""
    if utc_hour is None:
        return None
    return (utc_hour + offset_hours) % 24


# ── Construction du rapport ──────────────────────────────────────────────────

class DailyReportService:
    """Assemble et rend le rapport quotidien d'une ou plusieurs agences."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.analytics = AnalyticsService(db)

    async def build_reports(
        self,
        *,
        day: date,
        org_slug: str | None = None,
        branch_slug: str | None = None,
        offset_hours: int | None = None,
    ) -> list[dict]:
        """
        Construit le rapport de chaque agence active pour la journée demandée.

        Args:
            day: Journée locale visée
            org_slug: Restreint à une organisation (None = toutes)
            branch_slug: Restreint à une agence (None = toutes)
            offset_hours: Décalage UTC ; défaut `REPORT_LOCAL_UTC_OFFSET_HOURS`

        Returns:
            Liste de rapports, triée par organisation puis agence. Vide si
            aucune agence ne correspond — ce n'est pas une erreur : un filtre
            trop étroit doit se voir dans la sortie du CLI, pas lever.
        """
        offset = (
            settings.REPORT_LOCAL_UTC_OFFSET_HOURS if offset_hours is None else offset_hours
        )

        statement = (
            select(Branch, Organization)
            .join(Organization, Organization.id == Branch.org_id)
            .where(Branch.is_active.is_(True))
            .order_by(Organization.name, Branch.name)
        )
        if org_slug:
            statement = statement.where(Organization.slug == org_slug)
        if branch_slug:
            statement = statement.where(Branch.slug == branch_slug)

        rows = (await self.db.execute(statement)).all()
        return [
            await self._build_one(branch, organization, day, offset)
            for branch, organization in rows
        ]

    async def _build_one(
        self,
        branch: Branch,
        organization: Organization,
        day: date,
        offset_hours: int,
    ) -> dict:
        """Assemble un rapport pour une agence. Une agence sans données reste valide."""
        date_from, date_to = local_day_bounds(day, offset_hours)

        summary = await self.analytics.get_agency_summary(
            branch.org_id, branch.id, date_from, date_to
        )
        services = await self.analytics.get_service_stats(
            branch.org_id, branch.id, date_from, date_to
        )
        counters = await self.analytics.get_counter_performance(
            branch.org_id, branch.id, date_from, date_to
        )

        by_status = summary.get("tickets_by_status") or {}
        created = summary["total_tickets"]
        done = by_status.get("ticket_done", 0)
        cancelled = by_status.get("ticket_cancelled", 0)
        incomplete = by_status.get("ticket_incomplete", 0)
        transferred = by_status.get("ticket_transferred", 0)

        return {
            "date": day.isoformat(),
            "date_label": french_date(day),
            "organization": {"id": organization.id, "name": organization.name},
            "branch": {
                "id": branch.id,
                "name": branch.name,
                "slug": branch.slug,
                "city": branch.city,
                "email": branch.email,
            },
            "timezone_offset_hours": offset_hours,
            "period_utc": {"from": date_from.isoformat(), "to": date_to.isoformat()},
            "totals": {
                "created": created,
                "done": done,
                "cancelled": cancelled,
                "incomplete": incomplete,
                "transferred": transferred,
                "called": summary["total_called"],
                "absent": summary["total_absent"],
                "avg_wait_minutes": summary["avg_wait_minutes"],
                "absence_rate_percent": summary["absence_rate_percent"],
                "busiest_hour_local": _local_hour(summary.get("busiest_hour"), offset_hours),
            },
            # Trié par volume : le gérant lit la première ligne, pas la dernière.
            "services": sorted(
                services, key=lambda item: item["total_tickets"], reverse=True
            ),
            "counters": counters,
            "has_activity": created > 0 or summary["total_called"] > 0,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }


# ── Rendu texte ──────────────────────────────────────────────────────────────

def render_daily_report(report: dict) -> str:
    """
    Rend le rapport en texte brut, aligné, lisible sur un téléphone.

    Pas de markdown, pas de tableau : les clients mail en texte brut
    n'interprètent rien, et une colonne mal alignée reste lisible alors qu'un
    tableau HTML cassé ne l'est pas.
    """
    totals = report["totals"]
    branch = report["branch"]
    city = f" — {branch['city']}" if branch.get("city") else ""

    lines = [
        "TONDE — Rapport quotidien",
        f"{branch['name']}{city}",
        report["date_label"],
        "=" * 46,
    ]

    if not report["has_activity"]:
        lines += [
            "",
            "Aucune activité enregistrée ce jour.",
            "Aucun ticket n'a été créé ni appelé sur cette agence.",
        ]
    else:
        busiest = totals["busiest_hour_local"]
        busiest_text = f"{busiest}h00 (heure locale)" if busiest is not None else "—"
        lines += [
            "",
            "Activité",
            _line("Tickets créés", _number(totals["created"])),
            _line("Clients servis", _number(totals["done"])),
        ]

        # Les volumes annexes restent groupés avec les deux lignes ci-dessus :
        # un gérant lit « créés / servis / annulés » comme un seul bloc. Les
        # lignes à zéro sont tues — voir un « Annulés 0 » tous les jours finit
        # par faire ignorer le rapport entier.
        for label, key in (
            ("Annulés", "cancelled"),
            ("Inachevés", "incomplete"),
            ("Absents", "absent"),
            ("Transférés", "transferred"),
        ):
            if totals[key]:
                lines.append(_line(label, _number(totals[key])))

        lines += [
            _line("Temps d'attente moyen", f"{_number(totals['avg_wait_minutes'], 1)} min"),
            _line("Taux d'absence", f"{_number(totals['absence_rate_percent'], 1)} %"),
            _line("Heure de pointe", busiest_text),
        ]

        if report["services"]:
            lines += ["", "Par service"]
            for service in report["services"]:
                lines.append(
                    f"  {service['service_name'] or 'Service'} : "
                    f"{_number(service['total_tickets'])} ticket(s), "
                    f"attente {_number(service['avg_wait_minutes'], 1)} min, "
                    f"absence {_number(service['absence_rate_percent'], 1)} %"
                )

        if report["counters"]:
            lines += ["", "Par guichet"]
            for counter in report["counters"]:
                lines.append(
                    f"  {counter['counter_name']} : "
                    f"{_number(counter['tickets_done'])} client(s) servi(s)"
                )

    lines += [
        "",
        "=" * 46,
        f"Rapport généré automatiquement par TONDE. Réponse : {settings.SUPPORT_EMAIL}",
    ]
    return "\n".join(lines)


def report_subject(report: dict) -> str:
    return f"TONDE — Rapport du {report['date_label']} — {report['branch']['name']}"


# ── Envoi ────────────────────────────────────────────────────────────────────

async def deliver_reports(
    reports: list[dict],
    *,
    to: str | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """
    Envoie chaque rapport par email.

    Le destinataire par défaut est l'adresse de l'agence (`branches.email`).
    Sans adresse, ou sans SMTP configuré, le rapport n'est **pas** envoyé et la
    raison est renvoyée : un cron silencieux qui n'envoie rien est pire qu'un
    cron qui échoue bruyamment.

    Args:
        reports: Sortie de `DailyReportService.build_reports`
        to: Destinataire imposé (surcharge l'adresse de l'agence)
        dry_run: Rend le texte sans rien envoyer

    Returns:
        Une ligne de résultat par rapport : {branch, sent, reason?, error?}
    """
    results: list[dict] = []

    for report in reports:
        branch_name = report["branch"]["name"]
        recipient = to or report["branch"].get("email")

        if dry_run:
            results.append({"branch": branch_name, "sent": False, "reason": "dry_run"})
            continue

        if not recipient:
            logger.warning(f"Rapport {report['date']} non envoyé : {branch_name} sans email")
            results.append({"branch": branch_name, "sent": False, "reason": "no_recipient"})
            continue

        # En développement, `send_email` journalise au lieu d'envoyer : c'est le
        # comportement voulu, on ne bloque donc que hors développement.
        if not settings.is_development and not is_configured():
            results.append({
                "branch": branch_name,
                "sent": False,
                "reason": "smtp_not_configured",
            })
            continue

        try:
            await send_email(
                recipient,
                report_subject(report),
                render_daily_report(report),
                reply_to=settings.SUPPORT_EMAIL,
            )
        except EmailDeliveryError as exc:
            logger.error(f"Rapport {report['date']} non envoyé à {recipient} : {exc}")
            results.append({
                "branch": branch_name,
                "sent": False,
                "reason": "smtp_error",
                "error": str(exc),
            })
            continue

        results.append({
            "branch": branch_name,
            "sent": True,
            "to": recipient,
            # En développement l'email est journalisé, pas remis : le CLI doit
            # le dire, sinon « envoyé » est un mensonge qui se découvre le jour
            # où l'on attend un rapport qui n'arrive pas.
            "simulated": settings.is_development,
        })

    return results
