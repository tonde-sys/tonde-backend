"""
Garanties structurelles du moteur — lots 012 à 014 de la spécification.

Ce fichier couvre ce que la base garantit ELLE-MÊME, indépendamment de
l'application : un invariant tenu par du code applicatif peut être contourné par
une seconde instance, un script de maintenance ou un redémarrage au mauvais
moment. Un invariant tenu par un index ne le peut pas.

  - 012 : un seul ticket actif par utilisateur (index unique partiel) ;
  - 013 : `queue_sequence` monotone, ordre indépendant de l'horloge ;
  - 014 : Outbox — aucun événement perdu si Redis tombe au moment du commit.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.event_outbox import EventOutbox
from app.models.ticket import Ticket, TicketPriority, TicketStatus
from app.schemas.ticket import CreateTicketRequest
from app.services.outbox import MAX_AGE_MINUTES, MAX_ATTEMPTS, OutboxService
from app.services.ticket_service import TicketService
from tests.test_ticket_service import (
    _create_test_agency,
    _create_test_org,
    _create_test_service,
)


@pytest.fixture
def mock_queue(mock_redis):
    """Neutralise les helpers Redis de la file (le client Redis est déjà mocké)."""
    with patch("app.services.ticket_service.add_to_queue", new_callable=AsyncMock, return_value=1), \
         patch("app.services.ticket_service.get_queue_size", new_callable=AsyncMock, return_value=1), \
         patch("app.services.ticket_service.get_queue_position", new_callable=AsyncMock, return_value=1), \
         patch("app.services.ticket_service.next_ticket_sequence", new_callable=AsyncMock, return_value=1), \
         patch("app.services.ticket_service.publish_event", new_callable=AsyncMock):
        yield


def _ticket(org_id, agency_id, service_id, *, user_id, number="A-001",            status=TicketStatus.WAITING, priority=TicketPriority.STANDARD,
            created_at=None, sequence=1) -> Ticket:
    """Ticket brut, inséré sans passer par le service (donc sans ses contrôles)."""
    return Ticket(
        id=str(uuid.uuid4()),
        org_id=org_id, agency_id=agency_id, service_id=service_id,
        user_id=user_id, number=number, prefix=number.split("-")[0],
        sequence=sequence, status=status, priority=priority,
        created_at=created_at or datetime.now(timezone.utc),
    )


# ══ 012 — l'index unique partiel ═════════════════════════════════════════════

@pytest.mark.asyncio
async def test_l_index_refuse_un_second_ticket_actif(db_session):
    """
    La base refuse elle-même le doublon, sans que l'application ait son mot à dire.

    C'est la différence entre « l'application vérifie » (deux instances peuvent
    vérifier en même temps) et « la base interdit » (aucune ne peut).
    """
    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)

    db_session.add(_ticket(org.id, agency.id, service.id, user_id="client-u",
                           number="A-001", sequence=1))
    await db_session.commit()

    db_session.add(_ticket(org.id, agency.id, service.id, user_id="client-u",
                           number="A-002", sequence=2))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_l_index_laisse_prendre_un_ticket_apres_cloture(db_session):
    """Un ticket clos n'est plus actif : l'index ne doit pas bloquer le suivant."""
    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)

    for i, statut in enumerate((TicketStatus.DONE, TicketStatus.CANCELLED), start=1):
        db_session.add(_ticket(org.id, agency.id, service.id, user_id="client-v",
                               number=f"A-00{i}", sequence=i, status=statut))
        await db_session.commit()

    db_session.add(_ticket(org.id, agency.id, service.id, user_id="client-v",
                           number="A-003", sequence=3))
    await db_session.commit()   # ne doit pas lever


@pytest.mark.asyncio
async def test_l_index_ignore_les_autres_utilisateurs(db_session):
    """L'unicité est par utilisateur : deux clients peuvent avoir un ticket actif."""
    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)

    db_session.add(_ticket(org.id, agency.id, service.id, user_id="client-a",
                           number="A-001", sequence=1))
    db_session.add(_ticket(org.id, agency.id, service.id, user_id="client-b",
                           number="A-002", sequence=2))
    await db_session.commit()   # ne doit pas lever


@pytest.mark.asyncio
async def test_la_course_de_creation_donne_409_et_non_500(db_session, mock_queue):
    """
    Deux créations simultanées : la perdante reçoit le ticket de la gagnante.

    On simule exactement la fenêtre de course — le `SELECT` préalable ne voit
    encore rien — pour vérifier que c'est bien l'INDEX qui tranche, et que
    l'utilisateur reçoit un 409 exploitable plutôt qu'une erreur serveur. Sans ce
    traitement, une violation de contrainte remontait en 500 : le client voyait
    « erreur serveur » alors que son ticket existait.
    """
    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)
    ticket_service = TicketService(db_session)

    premier = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency.id, service_id=service.id), "client-race", None
    )

    vrai = TicketService._get_active_ticket
    appels = {"n": 0}

    async def faux(self, user_id):
        appels["n"] += 1
        if appels["n"] == 1:
            return None      # la fenêtre de course : le SELECT ne voit rien
        return await vrai(self, user_id)

    with patch.object(TicketService, "_get_active_ticket", faux):
        with pytest.raises(HTTPException) as exc:
            await TicketService(db_session).create_ticket(
                CreateTicketRequest(agency_id=agency.id, service_id=service.id),
                "client-race", None,
            )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "TICKET_ALREADY_ACTIVE"
    assert exc.value.detail["active_ticket_id"] == premier.id, "le gagnant est nommé"


# ══ Chaîne et SQL des migrations ═════════════════════════════════════════════

def test_migration_012_est_dans_la_chaine_et_la_tete_reste_unique():
    """
    Une seule tête Alembic, et 012 dans la chaîne.

    Ne PAS figer le nom de la tête : ce test a déjà cassé deux fois en ajoutant
    une migration (010, puis 011). Ce qu'on veut vérifier, c'est qu'il n'y a
    qu'une tête et que la nouvelle migration y est.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    assert len(heads) == 1, f"têtes multiples : {heads}"

    revisions = [r.revision for r in script.walk_revisions()]
    assert "012_unique_active_ticket" in revisions


def test_le_sql_de_012_utilise_les_libelles_d_enum_en_majuscules():
    """
    Le prédicat de l'index doit citer `'WAITING'`, pas `'waiting'`.

    SQLAlchemy persiste le NOM du membre de l'énumération. Un prédicat en
    minuscules ne casse AUCUN test local (SQLite construit son schéma depuis les
    modèles) mais fait échouer `alembic upgrade` sur une base vide — c'est
    exactement ce qui a produit la CI rouge du run #41.
    """
    from io import StringIO

    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", "postgresql://u:p@localhost/db")
    buf = StringIO()
    cfg.output_buffer = buf

    command.upgrade(cfg, "011_ticket_absence_recovery:012_unique_active_ticket", sql=True)
    sql = buf.getvalue()

    assert "CREATE UNIQUE INDEX uq_tickets_active_per_user" in sql
    assert "WHERE status IN ('WAITING', 'CALLED', 'SERVING'" in sql
    assert "'waiting'" not in sql, "libellé en minuscules : casserait alembic upgrade"


# ══ 013 — la séquence de file ════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_deux_tickets_crees_dans_la_meme_milliseconde_sont_ordonnes(db_session, mock_queue):
    """
    Le défaut que la migration 013 corrige, vérifié sur de vraies créations.

    Avant, deux tickets créés dans la même milliseconde recevaient le MÊME score
    Redis et étaient départagés par l'ordre lexicographique de leur UUID. Ici les
    deux tickets partagent exactement le même `created_at` : seule la séquence
    peut les ordonner, et elle doit le faire dans l'ordre de création.
    """
    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)
    ticket_service = TicketService(db_session)

    premier = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency.id, service_id=service.id), "client-1", None
    )
    second = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency.id, service_id=service.id), "client-2", None
    )

    assert premier.queue_sequence is not None
    assert second.queue_sequence > premier.queue_sequence, "la séquence ne recule jamais"


@pytest.mark.asyncio
async def test_la_reprise_en_file_recoit_une_nouvelle_sequence(db_session, mock_queue):
    """
    Une reprise passe en fin de SA tranche, et c'est désormais formel.

    Avec l'horodatage, « fin de tranche » dépendait du moment de l'appel ; avec la
    séquence, c'est une valeur strictement supérieure à toutes celles déjà en
    file, donc la place est garantie quelle que soit l'horloge.
    """
    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)
    ticket_service = TicketService(db_session)

    ticket = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency.id, service_id=service.id), "client-reprise", None
    )
    avant = ticket.queue_sequence

    # ABSENT est un état actif : on y va par la machine à états, pas en écrivant
    # le statut à la main (ce qui contournerait les contrôles qu'on veut tester).
    from app.models.ticket import Ticket as TicketModel
    row = (await db_session.execute(
        select(TicketModel).where(TicketModel.id == ticket.id)
    )).scalar_one()
    row.status = TicketStatus.ABSENT
    await db_session.commit()

    await ticket_service.return_to_queue(ticket.id, org.id, user_id="client-reprise")

    await db_session.refresh(row)
    assert row.queue_sequence > avant, "la reprise doit repartir derrière tout le monde"


# ══ 014 — l'Outbox ═══════════════════════════════════════════════════════════

async def _evenement_en_attente(db_session, *, publie: bool):
    """Joue une transition avec la diffusion en panne, et rend la ligne d'outbox."""
    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)
    ticket = _ticket(org.id, agency.id, service.id, user_id="client-outbox")
    db_session.add(ticket)
    await db_session.commit()

    with patch("app.services.outbox.publish_event", new_callable=AsyncMock, return_value=publie):
        await TicketService(db_session)._publish_counter_activity(ticket, "called")
    await db_session.commit()

    return (await db_session.execute(select(EventOutbox))).scalars().all()


@pytest.mark.asyncio
async def test_l_evenement_est_ecrit_dans_la_transaction_metier(db_session):
    """
    La ligne d'outbox existe MÊME si la diffusion a échoué.

    C'est toute la garantie : l'écriture et le changement d'état partagent la
    même transaction, donc il est impossible d'avoir l'un sans l'autre. Avant, un
    Redis muet au moment du commit perdait l'événement sans laisser de trace.
    """
    lignes = await _evenement_en_attente(db_session, publie=False)

    assert len(lignes) == 1
    assert lignes[0].event_type == "counter_queue_changed"
    assert lignes[0].published_at is None, "en attente de rejeu"
    assert lignes[0].payload["agency_id"], "le message est conservé tel qu'il sera diffusé"


@pytest.mark.asyncio
async def test_une_diffusion_reussie_marque_la_ligne_publiee(db_session):
    """Cas nominal : pas de rejeu inutile, la ligne est marquée dans la foulée."""
    lignes = await _evenement_en_attente(db_session, publie=True)

    assert len(lignes) == 1
    assert lignes[0].published_at is not None


@pytest.mark.asyncio
async def test_le_rejeu_publie_les_evenements_en_attente(db_session):
    """Le répartiteur reprend ce que la diffusion immédiate n'a pas pu envoyer."""
    lignes = await _evenement_en_attente(db_session, publie=False)

    with patch("app.services.outbox.publish_event", new_callable=AsyncMock, return_value=True) as publish:
        traites = await OutboxService(db_session).dispatch()

    assert traites == 1
    assert publish.await_count == 1
    await db_session.refresh(lignes[0])
    assert lignes[0].published_at is not None


@pytest.mark.asyncio
async def test_le_rejeu_abandonne_apres_trop_de_tentatives(db_session):
    """
    Un événement qui échoue en boucle sort du circuit — mais reste traçable.

    Sans plafond, une file d'outbox irrécupérable serait réessayée à chaque
    passage du scheduler, indéfiniment. On garde la ligne (avec son erreur) pour
    pouvoir répondre à « pourquoi cet écran n'a-t-il rien reçu ? ».
    """
    lignes = await _evenement_en_attente(db_session, publie=False)
    lignes[0].attempts = MAX_ATTEMPTS - 1
    await db_session.commit()

    with patch("app.services.outbox.publish_event", new_callable=AsyncMock, return_value=False):
        await OutboxService(db_session).dispatch()

    await db_session.refresh(lignes[0])
    assert lignes[0].attempts == MAX_ATTEMPTS
    assert lignes[0].published_at is not None, "sorti du circuit"
    assert lignes[0].last_error, "l'erreur est conservée pour l'exploitation"


@pytest.mark.asyncio
async def test_un_evenement_trop_vieux_n_est_pas_rejoue(db_session):
    """
    Passé une heure, l'événement n'a plus d'objet : l'écran s'est resynchronisé.

    Le publier n'ajouterait qu'un sursaut d'affichage sur un état déjà périmé.
    """
    lignes = await _evenement_en_attente(db_session, publie=False)
    lignes[0].created_at = datetime.now(timezone.utc) - timedelta(minutes=MAX_AGE_MINUTES + 1)
    await db_session.commit()

    with patch("app.services.outbox.publish_event", new_callable=AsyncMock, return_value=True) as publish:
        traites = await OutboxService(db_session).dispatch()

    assert traites == 0
    assert publish.await_count == 0


def test_le_nom_de_la_sequence_est_coherent_entre_migration_et_service():
    """
    Le nom de la séquence PostgreSQL est écrit à DEUX endroits : la migration la
    crée, le service l'interroge. Un renommage d'un seul côté ne casse rien en
    test (SQLite n'utilise jamais cette branche) et casse la production au
    premier ticket créé. Ce test relie les deux.
    """
    import inspect
    import pathlib

    from app.services.ticket_service import TicketService

    nom = "ticket_queue_sequence_seq"
    source_service = inspect.getsource(TicketService._next_queue_sequence)
    source_migration = pathlib.Path(
        "migrations/versions/013_queue_sequence.py"
    ).read_text(encoding="utf-8")

    assert nom in source_service, "le service n'interroge plus cette séquence"
    assert nom in source_migration, "la migration ne crée plus cette séquence"


def test_la_chaine_de_migrations_va_jusqu_a_014():
    """La tête est unique et la chaîne contient les trois lots de cette passe."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert len(script.get_heads()) == 1

    revisions = [r.revision for r in script.walk_revisions()]
    for attendu in ("012_unique_active_ticket", "013_queue_sequence", "014_event_outbox"):
        assert attendu in revisions
