"""
Algorithme d'absence et de reprise — spécification Queue Engine v1.0.

Trois défauts réels sont corrigés ici, et ce fichier est leur preuve :

  1. `mark_absent` ne vérifiait AUCUN délai : un agent pouvait déclarer absent un
     client une seconde après l'avoir appelé. Le client qui traversait la salle
     se retrouvait derrière tout le monde pour avoir marché trop lentement.
  2. La fenêtre de récupération était recalculée depuis `called_at` (l'APPEL) au
     lieu d'être ancrée sur l'ABSENCE — 180 s d'écart — et `called_at` étant
     écrasé à chaque appel, une reprise suivie d'un nouvel appel repoussait
     silencieusement l'échéance.
  3. Aucun `missed_count` : un client pouvait revenir indéfiniment, à deux SMS et
     180 s de guichet par cycle.

Les valeurs de la spécification (§3.2) sont reprises : grâce 90 s, fenêtre 30 min,
deux absences maximum. Elles vivent dans `settings`, pas en dur.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.config import settings
from app.models.audit_log import AuditAction, AuditLog
from app.models.employee import EmployeeStatus
from app.models.organization import Organization
from app.models.ticket import Ticket, TicketPriority, TicketStatus
from app.models.user import User, UserRole
from app.schemas.ticket import CreateTicketRequest
from app.services.ticket_service import TicketService, as_utc
from tests.test_phase6_counter_channel import (  # noqa: F401 — fixture réutilisée
    _agent_au_guichet,
    _employee,
    _org,
    _scene_guichets,
    _superviseur,
    _user,
    canaux,
)
from tests.test_queue_atomicity import _make_counter


# ── Fabriques locales ────────────────────────────────────────────────────────

async def _ticket(db, service: TicketService, scene: dict) -> Ticket:
    """Ticket neuf en WAITING au guichet A1."""
    created = await service.create_ticket(
        CreateTicketRequest(agency_id=scene["agency_a"].id, service_id=scene["service_a"].id),
        "client-1",
        scene["org_a"].id,
    )
    return (await db.execute(
        select(Ticket).where(Ticket.id == created.id).execution_options(populate_existing=True)
    )).scalar_one()


async def _appele(
    db, service: TicketService, scene: dict, *,
    grace_seconds: float = -1,
    missed: int = 0,
    priority: TicketPriority = TicketPriority.STANDARD,
    recovery_seconds: float | None = None,
) -> Ticket:
    """
    Ticket en CALLED, avec les échéances posées explicitement.

    `grace_seconds` négatif = la grâce est DÉJÀ écoulée (cas normal d'un agent
    qui a attendu). Positif = il reste ce temps avant que l'absence soit permise.

    Ces valeurs sont écrites à la main plutôt que calculées par `call_next` :
    un test de borne ne doit pas dépendre du chemin qui produit la borne.
    """
    ticket = await _ticket(db, service, scene)
    now = datetime.now(timezone.utc)
    ticket.status = TicketStatus.CALLED
    ticket.counter_id = scene["counter_a"].id
    ticket.counter_name = scene["counter_a"].name
    ticket.called_at = now
    ticket.priority = priority
    ticket.missed_count = missed
    ticket.grace_expires_at = now + timedelta(seconds=grace_seconds)
    if recovery_seconds is not None:
        ticket.recovery_expires_at = now + timedelta(seconds=recovery_seconds)
    await db.commit()
    await db.refresh(ticket)
    return ticket


async def _absent(
    db, service: TicketService, scene: dict, *,
    missed: int = 1,
    recovery_seconds: float = 600,
    priority: TicketPriority = TicketPriority.STANDARD,
) -> Ticket:
    """Ticket en ABSENT, dans (ou hors) sa fenêtre de récupération."""
    ticket = await _appele(db, service, scene, priority=priority)
    now = datetime.now(timezone.utc)
    ticket.status = TicketStatus.ABSENT
    ticket.missed_count = missed
    ticket.last_missed_at = now
    ticket.grace_expires_at = None
    ticket.recovery_expires_at = now + timedelta(seconds=recovery_seconds)
    ticket.counter_id = None
    ticket.counter_name = None
    await db.commit()
    await db.refresh(ticket)
    return ticket


async def _ticket_brut(db, scene: dict, *, number: str = "A-999") -> Ticket:
    """
    Insère un ticket directement, sans passer par le service.

    `create_ticket` refuse un second ticket actif — c'est exactement la règle
    qu'on veut contourner ici, pour simuler une donnée incohérente héritée d'une
    époque où elle n'était pas garantie en base.
    """
    ticket = Ticket(
        org_id=scene["org_a"].id,
        number=number,
        prefix="A",
        sequence=int(number.split("-")[1]),
        user_id="client-1",
        agency_id=scene["agency_a"].id,
        service_id=scene["service_a"].id,
        status=TicketStatus.WAITING,
        priority=TicketPriority.STANDARD,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    return ticket


# ══════════════════════════════════════════════════════════════════════════════
# §3.1 — Délai de grâce
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_l_appel_pose_une_echeance_de_grace(db_session, canaux):
    """
    Le délai de grâce doit être une DONNÉE écrite au moment de l'appel, pas un
    calcul refait à chaque lecture : c'est ce qui rend l'échéance vérifiable
    après un redémarrage, et impossible à repousser par un nouvel appel.
    """
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    agent = await _agent_au_guichet(db_session, scene)
    ticket = await _ticket(db_session, service, scene)

    with patch("app.services.ticket_service.pop_next_ticket",
               new_callable=AsyncMock, return_value=(ticket.id, 1.0)), \
         patch("app.services.ticket_service.restore_to_queue", new_callable=AsyncMock):
        await service.call_next(
            agency_id=scene["agency_a"].id, service_id=scene["service_a"].id,
            counter_id=scene["counter_a"].id, org_id=scene["org_a"].id, actor=agent,
        )

    appele = (await db_session.execute(
        select(Ticket).where(Ticket.id == ticket.id).execution_options(populate_existing=True)
    )).scalar_one()

    assert appele.status == TicketStatus.CALLED
    assert appele.grace_expires_at is not None
    ecart = (appele.grace_expires_at - appele.called_at).total_seconds()
    assert abs(ecart - settings.GRACE_PERIOD_SECONDS) < 2


@pytest.mark.asyncio
async def test_absence_refusee_pendant_la_grace(db_session, canaux):
    """
    Le cas d'usage : le client marche vers le guichet. L'agent ne doit pas
    pouvoir le déclarer absent avant la fin du délai — c'est la règle de la
    spécification, et c'est aussi le bon sens.
    """
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    agent = await _agent_au_guichet(db_session, scene)
    ticket = await _appele(db_session, service, scene, grace_seconds=60)

    with pytest.raises(HTTPException) as exc:
        await service.mark_absent(ticket.id, agent)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "GRACE_PERIOD_NOT_ELAPSED"
    assert 0 < exc.value.detail["seconds_left"] <= 61
    assert exc.value.detail["grace_period_seconds"] == settings.GRACE_PERIOD_SECONDS

    # Rien n'a bougé : un refus ne doit pas laisser le ticket à moitié absent.
    inchange = (await db_session.execute(
        select(Ticket).where(Ticket.id == ticket.id).execution_options(populate_existing=True)
    )).scalar_one()
    assert inchange.status == TicketStatus.CALLED
    assert inchange.missed_count == 0


@pytest.mark.asyncio
async def test_absence_acceptee_apres_la_grace(db_session, canaux):
    """La grâce écoulée, l'agent reprend la main : c'est lui qui confirme."""
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    agent = await _agent_au_guichet(db_session, scene)
    ticket = await _appele(db_session, service, scene, grace_seconds=-1)

    result = await service.mark_absent(ticket.id, agent)

    assert result["success"] is True
    assert result["missed_count"] == 1
    assert result["recovery_allowed"] is True

    absent = (await db_session.execute(
        select(Ticket).where(Ticket.id == ticket.id).execution_options(populate_existing=True)
    )).scalar_one()
    assert absent.status == TicketStatus.ABSENT
    assert absent.missed_count == 1
    assert absent.last_missed_at is not None
    assert absent.grace_expires_at is None, "la grâce concerne l'appel, pas l'absence"


@pytest.mark.asyncio
async def test_un_superviseur_peut_deroger_et_c_est_audite(db_session, canaux):
    """
    Spécification §3.1 : une dérogation est possible, mais elle est AUDITÉE avec
    son motif. Un agent, lui, n'a pas autorité pour raccourcir le délai qu'on lui
    impose — sans quoi le contrôle ne serait qu'une politesse.
    """
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    superviseur = await _superviseur(db_session, scene)
    ticket = await _appele(db_session, service, scene, grace_seconds=60)

    result = await service.mark_absent(ticket.id, superviseur)

    assert result["success"] is True
    assert result["missed_count"] == 1

    traces = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == AuditAction.TICKET_ABSENT_OVERRIDE)
    )).scalars().all()
    assert len(traces) == 1
    assert traces[0].actor_id == superviseur.id
    assert traces[0].target_id == ticket.id
    assert traces[0].extra_data["seconds_left"] > 0
    assert traces[0].extra_data["grace_period_seconds"] == settings.GRACE_PERIOD_SECONDS


@pytest.mark.asyncio
async def test_une_relance_repose_la_grace(db_session, canaux):
    """
    Relancer puis déclarer absent dans la seconde serait un contournement du
    contrôle : la relance est une nouvelle annonce, donc un nouveau délai.
    """
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    agent = await _agent_au_guichet(db_session, scene)
    ticket = await _appele(db_session, service, scene, grace_seconds=-5)

    await service.recall(ticket.id, agent)

    avec_relance = (await db_session.execute(
        select(Ticket).where(Ticket.id == ticket.id).execution_options(populate_existing=True)
    )).scalar_one()
    # SQLite rend des datetimes naïfs : `as_utc` est le helper du service, il
    # évite d'écrire une seconde convention de fuseau dans les tests.
    restant = (as_utc(avec_relance.grace_expires_at) - datetime.now(timezone.utc)).total_seconds()
    assert restant > settings.GRACE_PERIOD_SECONDS - 5

    with pytest.raises(HTTPException) as exc:
        await service.mark_absent(ticket.id, agent)
    assert exc.value.detail["code"] == "GRACE_PERIOD_NOT_ELAPSED"


# ══════════════════════════════════════════════════════════════════════════════
# §3.4 — Reprise
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_reprise_acceptee_dans_la_fenetre(db_session, canaux):
    """Le ticket revient en file : même ticket, même numéro, compteur intact."""
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    agent = await _agent_au_guichet(db_session, scene)
    ticket = await _absent(db_session, service, scene, missed=1, recovery_seconds=600)

    result = await service.return_to_queue(ticket.id, scene["org_a"].id, actor=agent)

    assert result["success"] is True
    assert result["missed_count"] == 1

    repris = (await db_session.execute(
        select(Ticket).where(Ticket.id == ticket.id).execution_options(populate_existing=True)
    )).scalar_one()
    assert repris.status == TicketStatus.WAITING
    assert repris.number == ticket.number, "une reprise ne change jamais le numéro public"
    assert repris.requeued_at is not None
    assert repris.recovery_expires_at is None, "l'échéance ne doit plus être active"


@pytest.mark.asyncio
async def test_reprise_refusee_hors_fenetre(db_session, canaux):
    """La fenêtre écoulée, revenir n'a plus de sens : le ticket est en fin de vie."""
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    agent = await _agent_au_guichet(db_session, scene)
    ticket = await _absent(db_session, service, scene, recovery_seconds=-1)

    with pytest.raises(HTTPException) as exc:
        await service.return_to_queue(ticket.id, scene["org_a"].id, actor=agent)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "RECOVERY_WINDOW_EXPIRED"


@pytest.mark.asyncio
async def test_reprise_refusee_apres_le_plafond_d_absences(db_session, canaux):
    """
    Deux absences, puis plus de reprise. Sans plafond, un client pouvait revenir
    indéfiniment : deux SMS et 180 s de guichet par cycle, sans fin.
    """
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    agent = await _agent_au_guichet(db_session, scene)
    ticket = await _absent(
        db_session, service, scene, missed=settings.MAX_MISSED_COUNT, recovery_seconds=600
    )

    with pytest.raises(HTTPException) as exc:
        await service.return_to_queue(ticket.id, scene["org_a"].id, actor=agent)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "MAX_MISSED_COUNT_REACHED"
    assert exc.value.detail["missed_count"] == settings.MAX_MISSED_COUNT
    assert exc.value.detail["max_missed_count"] == settings.MAX_MISSED_COUNT


@pytest.mark.asyncio
async def test_reprise_conserve_la_priorite(db_session, canaux):
    """
    Spécification §4.3 : le ticket revient en fin de SA tranche. Le code
    précédent forçait STANDARD à vie — une femme enceinte perdait définitivement
    sa priorité après un seul appel manqué. La pénalité de temps (retour en fin
    de tranche) suffit ; la double peine n'ajoutait qu'une injustice.
    """
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    agent = await _agent_au_guichet(db_session, scene)
    ticket = await _absent(
        db_session, service, scene, missed=1, recovery_seconds=600,
        priority=TicketPriority.PRIORITY,
    )

    result = await service.return_to_queue(ticket.id, scene["org_a"].id, actor=agent)

    assert result["priority"] == TicketPriority.PRIORITY.value
    repris = (await db_session.execute(
        select(Ticket).where(Ticket.id == ticket.id).execution_options(populate_existing=True)
    )).scalar_one()
    assert repris.priority == TicketPriority.PRIORITY


@pytest.mark.asyncio
async def test_reprise_place_le_ticket_en_fin_de_sa_tranche(db_session, canaux):
    """
    L'insertion se fait sans `created_ms` : le score est donc
    `(10 - priorité) × marge + maintenant`, c'est-à-dire APRÈS tous les tickets
    de même priorité déjà en file. C'est exactement la règle de courtoisie
    (`REQUEUE_AHEAD_COUNT`) — elle est satisfaite par construction, sans
    réécrire la séquence d'aucun autre ticket.
    """
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    agent = await _agent_au_guichet(db_session, scene)
    ticket = await _absent(
        db_session, service, scene, missed=1, recovery_seconds=600,
        priority=TicketPriority.VIP,
    )

    with patch("app.services.ticket_service.add_to_queue", new_callable=AsyncMock) as ajout:
        await service.return_to_queue(ticket.id, scene["org_a"].id, actor=agent)

    ajout.assert_awaited_once()
    args, kwargs = ajout.call_args
    assert args[4] == 5, "score de priorité VIP (PRIORITY_SCORES : VIP = 5)"
    assert kwargs.get("created_ms") is None, "None = maintenant = fin de la tranche"


@pytest.mark.asyncio
async def test_reprise_refusee_si_un_autre_ticket_est_actif(db_session, canaux):
    """
    L'état incohérent contre lequel ce garde-fou luttait est devenu IMPOSSIBLE.

    `return_to_queue` refusait la reprise si le client avait déjà un autre ticket
    actif — une donnée héritée d'une époque où la règle n'était garantie ni en
    base ni atomiquement. Depuis la migration 012, l'index unique partiel
    interdit deux tickets actifs pour un même client : l'insertion elle-même
    échoue, même en écrivant directement en base, sans passer par le service.

    Le contrôle applicatif reste en place (défense en profondeur, et bases sur
    lesquelles l'index n'aurait pas encore été appliqué), mais il n'est plus le
    seul rempart : c'est la base qui refuse. On vérifie donc le refus là où il
    se produit désormais.
    """
    from sqlalchemy.exc import IntegrityError

    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    await _absent(db_session, service, scene, recovery_seconds=600)

    with pytest.raises(IntegrityError):
        await _ticket_brut(db_session, scene)   # WAITING, même client → refusé

    await db_session.rollback()


@pytest.mark.asyncio
async def test_le_compteur_d_absences_s_accumule(db_session, canaux):
    """
    Deux cycles complets : appel → absent → reprise → appel → absent. Le
    compteur doit valoir 2, et la seconde reprise doit être refusée.
    """
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    agent = await _agent_au_guichet(db_session, scene)
    ticket = await _appele(db_session, service, scene, grace_seconds=-1)

    premier = await service.mark_absent(ticket.id, agent)
    assert premier["missed_count"] == 1

    await service.return_to_queue(ticket.id, scene["org_a"].id, actor=agent)

    # Second appel : le ticket est de nouveau en CALLED, grâce écoulée.
    now = datetime.now(timezone.utc)
    ticket = (await db_session.execute(
        select(Ticket).where(Ticket.id == ticket.id).execution_options(populate_existing=True)
    )).scalar_one()
    ticket.status = TicketStatus.CALLED
    ticket.counter_id = scene["counter_a"].id
    ticket.counter_name = scene["counter_a"].name
    ticket.called_at = now
    ticket.grace_expires_at = now - timedelta(seconds=1)
    await db_session.commit()

    second = await service.mark_absent(ticket.id, agent)
    assert second["missed_count"] == 2
    assert second["recovery_allowed"] is False

    with pytest.raises(HTTPException) as exc:
        await service.return_to_queue(ticket.id, scene["org_a"].id, actor=agent)
    assert exc.value.detail["code"] == "MAX_MISSED_COUNT_REACHED"


# ══════════════════════════════════════════════════════════════════════════════
# §3.6 — Expiration
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_expiration_utilise_l_echeance_de_reprise(db_session, canaux):
    """Le worker se fonde sur `recovery_expires_at`, la donnée, pas sur un calcul."""
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    ticket = await _absent(db_session, service, scene, recovery_seconds=-1)

    counts = await service.expire_stale_tickets()

    assert counts["absent"] == 1
    expire = (await db_session.execute(
        select(Ticket).where(Ticket.id == ticket.id).execution_options(populate_existing=True)
    )).scalar_one()
    assert expire.status == TicketStatus.CANCELLED


@pytest.mark.asyncio
async def test_un_appel_ancien_n_expire_pas_une_fenetre_encore_ouverte(db_session, canaux):
    """
    LE bug de l'ancienne règle : l'expiration comparait `called_at` à
    `now - 30 min`. Un ticket appelé il y a 40 minutes, absent depuis 2 minutes
    et donc encore dans sa fenêtre était annulé — alors que le client avait
    encore 28 minutes pour revenir.
    """
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    ticket = await _absent(db_session, service, scene, recovery_seconds=600)

    # Appel très ancien, absence récente : la fenêtre court depuis l'ABSENCE.
    ticket.called_at = datetime.now(timezone.utc) - timedelta(minutes=40)
    ticket.last_missed_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    await db_session.commit()

    counts = await service.expire_stale_tickets()

    assert counts["absent"] == 0
    intact = (await db_session.execute(
        select(Ticket).where(Ticket.id == ticket.id).execution_options(populate_existing=True)
    )).scalar_one()
    assert intact.status == TicketStatus.ABSENT


@pytest.mark.asyncio
async def test_un_absent_sans_echeance_reste_expirable(db_session, canaux):
    """
    Ligne héritée d'avant la migration 011 : `recovery_expires_at` NULL. Sans la
    règle de repli, ce ticket serait IMMORTEL et bloquerait son client à vie —
    un correctif qui crée des zombies n'est pas un correctif.
    """
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    ticket = await _absent(db_session, service, scene, recovery_seconds=600)

    ticket.recovery_expires_at = None
    ticket.called_at = datetime.now(timezone.utc) - timedelta(minutes=45)
    await db_session.commit()

    counts = await service.expire_stale_tickets()

    assert counts["absent"] == 1


# ══════════════════════════════════════════════════════════════════════════════
# Invariants de sélection
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_un_absent_dans_la_file_redis_n_est_jamais_appele(db_session, canaux):
    """
    Spécification §11.1.9. Même si une entrée ABSENT traînait dans le ZSET (après
    une panne Redis au mauvais moment), le compare-and-set PostgreSQL exige
    `status = waiting` : le ticket est ignoré, pas appelé.
    """
    scene = await _scene_guichets(db_session)
    service = TicketService(db_session)
    agent = await _agent_au_guichet(db_session, scene)
    absent = await _absent(db_session, service, scene, recovery_seconds=600)

    with patch("app.services.ticket_service.pop_next_ticket",
               new_callable=AsyncMock, return_value=(absent.id, 1.0)), \
         patch("app.services.ticket_service.restore_to_queue", new_callable=AsyncMock):
        result = await service.call_next(
            agency_id=scene["agency_a"].id, service_id=scene["service_a"].id,
            counter_id=scene["counter_a"].id, org_id=scene["org_a"].id, actor=agent,
        )

    assert result["success"] is False
    inchange = (await db_session.execute(
        select(Ticket).where(Ticket.id == absent.id).execution_options(populate_existing=True)
    )).scalar_one()
    assert inchange.status == TicketStatus.ABSENT


def test_une_grace_superieure_au_filet_est_refusee():
    """
    Si la grâce dépassait le filet automatique, le bouton « Client absent »
    s'ouvrirait après que le système a déjà déclaré l'absence : la grâce ne
    servirait à rien et `GRACE_PERIOD_NOT_ELAPSED` deviendrait inatteignable.
    La configuration doit être refusée au démarrage, pas découverte en prod.
    """
    from app.core.config import Settings

    with pytest.raises(ValueError, match="GRACE_PERIOD_SECONDS"):
        Settings(
            GRACE_PERIOD_SECONDS=300,
            ABSENT_TIMEOUT_SECONDS=180,
            ABSENT_WARNING_SECONDS=120,
        )


def test_la_migration_011_ferme_la_chaine():
    """La chaîne Alembic garde UNE seule tête, et 011 suit 010."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert len(script.get_heads()) == 1, f"têtes multiples : {script.get_heads()}"

    revision = script.get_revision("011_ticket_absence_recovery")
    assert revision is not None
    assert revision.down_revision == "010_unique_active_service_prefix"


def test_le_sql_des_migrations_utilise_les_libelles_d_enum_en_majuscules():
    """
    Les libellés des types enum PostgreSQL sont les NOMS des membres du modèle,
    en MAJUSCULES (migration 007 les a alignés ainsi). Écrire `status = 'absent'`
    dans une migration échoue avec « invalid input value for enum ticketstatus ».

    Ce défaut est INVISIBLE en local : SQLite construit son schéma depuis les
    modèles et n'exécute jamais les migrations. Il n'a été vu qu'en CI. Ce test
    rend le SQL hors ligne (aucune connexion) pour que la prochaine fois, il
    tombe en local.
    """
    import io
    from contextlib import redirect_stdout

    from alembic import command
    from alembic.config import Config

    # On ne rend QUE la plage 010 → 011 : la migration 008 appelle `sa.inspect()`,
    # qui n'a pas de sens en mode hors ligne. Rendre la chaîne entière échouerait
    # pour une raison sans rapport avec ce qu'on vérifie ici.
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        command.upgrade(
            Config("alembic.ini"),
            "010_unique_active_service_prefix:011_ticket_absence_recovery",
            sql=True,
        )
    sql = buffer.getvalue()

    assert "status = 'ABSENT'" in sql, "le backfill doit viser le libellé réel"
    assert "status = 'absent'" not in sql, "libellé minuscule : échec garanti sur PostgreSQL"
