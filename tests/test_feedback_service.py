"""
Retours utilisateurs : envoi, quota, doublons, rattachement et historique.

Ce que ces tests protègent, dans l'ordre d'importance :

1. Un retour arrive bien en base et ressort sous une forme que l'application
   comprend (catégorie et statut en minuscules, jamais les noms d'ENUM Python).
2. Le quota horaire compte par COMPTE et non par IP — un cybercafé ne doit pas
   bloquer ses clients parce qu'un seul d'entre eux a spammé.
3. Un même ticket ne peut pas être signalé deux fois dans la même catégorie,
   mais reste signalable dans une autre (un avis et un bug, ce n'est pas pareil).
4. `ticket_id` et `org_id` envoyés par le client ne sont pas crus sur parole.
5. « Mes retours » est paginé et trié du plus récent au plus ancien.
"""
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.feedback import Feedback, FeedbackCategory, FeedbackStatus
from app.models.ticket import Ticket, TicketStatus
from app.models.user import User, UserRole
from app.schemas.feedback import CreateFeedbackRequest
from app.services.feedback_service import FeedbackService
from tests.test_ticket_service import (
    _create_test_agency,
    _create_test_org,
    _create_test_service,
)


# ── Aides ─────────────────────────────────────────────────────────────────────

def _message(suffix: str = "") -> str:
    """Message valide (>= 20 caractères) et unique par suffixe."""
    return f"Le guichet trois etait ferme ce matin, personne pour me servir{suffix}"


async def _make_user(db, org_id: str | None = None) -> User:
    user = User(
        phone=f"+257{uuid.uuid4().int % 10**8:08d}",
        role=UserRole.CLIENT,
        org_id=org_id,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_ticket(db, org_id: str, user_id: str) -> Ticket:
    branch = await _create_test_agency(db, org_id)
    service = await _create_test_service(db, branch.id, org_id)
    ticket = Ticket(
        org_id=org_id,
        user_id=user_id,
        agency_id=branch.id,
        service_id=service.id,
        number="A-001",
        prefix="A",
        sequence=1,
        queue_sequence=1,
        status=TicketStatus.WAITING,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    return ticket


def _payload(**overrides) -> CreateFeedbackRequest:
    base = {"category": "review", "message": _message(), "rating": 4}
    base.update(overrides)
    return CreateFeedbackRequest(**base)


# ── Envoi ─────────────────────────────────────────────────────────────────────

async def test_un_avis_est_enregistre_et_relu(db_session):
    org = await _create_test_org(db_session)
    user = await _make_user(db_session, org.id)
    service = FeedbackService(db_session)

    data = await service.create(user, _payload(org_id=org.id))

    assert data["category"] == "review"
    assert data["status"] == "received"
    assert data["rating"] == 4
    assert data["org_id"] == org.id
    # Le message est nettoyé avant stockage : plus d'espaces de bord.
    assert data["message"] == _message()

    stored = (await db_session.execute(select(Feedback))).scalars().all()
    assert len(stored) == 1
    # En base, c'est bien le NOM de l'ENUM (majuscules) que SQLAlchemy écrit.
    assert stored[0].status == FeedbackStatus.RECEIVED
    assert stored[0].category == FeedbackCategory.REVIEW


async def test_un_retour_sans_ticket_peut_etre_repete(db_session):
    # Un avis sur l'application ne porte sur aucun passage : rien ne justifie
    # d'interdire d'en envoyer un second après une mise à jour.
    org = await _create_test_org(db_session)
    user = await _make_user(db_session, org.id)
    service = FeedbackService(db_session)

    for _ in range(2):
        await service.create(
            user, _payload(category="suggestion", message=_message(), rating=None)
        )

    total = len((await db_session.execute(select(Feedback))).scalars().all())
    assert total == 2


# ── Quota ─────────────────────────────────────────────────────────────────────

async def test_le_quota_horaire_bloque_le_sixieme_retour(db_session):
    org = await _create_test_org(db_session)
    user = await _make_user(db_session, org.id)
    service = FeedbackService(db_session)

    for index in range(5):
        await service.create(user, _payload(message=_message(f" ({index})")))

    with pytest.raises(HTTPException) as exc:
        await service.create(user, _payload(message=_message(" (6)")))

    assert exc.value.status_code == 429
    assert exc.value.detail["code"] == "FEEDBACK_RATE_LIMITED"


async def test_le_quota_est_par_compte_et_non_par_machine(db_session):
    # Deux utilisateurs du même téléphone partagé (cybercafé) ne se gênent pas.
    org = await _create_test_org(db_session)
    premier = await _make_user(db_session, org.id)
    second = await _make_user(db_session, org.id)
    service = FeedbackService(db_session)

    for index in range(5):
        await service.create(premier, _payload(message=_message(f" ({index})")))

    # Le premier est bloqué…
    with pytest.raises(HTTPException):
        await service.create(premier, _payload(message=_message(" (6)")))

    # …le second, non.
    data = await service.create(second, _payload(message=_message(" autre")))
    assert data["status"] == "received"


# ── Doublons ──────────────────────────────────────────────────────────────────

async def test_un_doublon_sur_le_meme_ticket_est_refuse(db_session):
    org = await _create_test_org(db_session)
    user = await _make_user(db_session, org.id)
    ticket = await _make_ticket(db_session, org.id, user.id)
    service = FeedbackService(db_session)

    await service.create(user, _payload(ticket_id=ticket.id))

    with pytest.raises(HTTPException) as exc:
        await service.create(user, _payload(ticket_id=ticket.id, message=_message(" bis")))

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "FEEDBACK_ALREADY_SENT"


async def test_une_autre_categorie_sur_le_meme_ticket_reste_possible(db_session):
    # Un avis sur le passage et un signalement de bug ne disent pas la même
    # chose : les fusionner ferait perdre l'un des deux.
    org = await _create_test_org(db_session)
    user = await _make_user(db_session, org.id)
    ticket = await _make_ticket(db_session, org.id, user.id)
    service = FeedbackService(db_session)

    await service.create(user, _payload(ticket_id=ticket.id))
    data = await service.create(
        user,
        _payload(category="bug", ticket_id=ticket.id, rating=None, message=_message(" bug")),
    )

    assert data["category"] == "bug"


# ── Rattachement ──────────────────────────────────────────────────────────────

async def test_le_ticket_d_un_autre_utilisateur_est_introuvable(db_session):
    org = await _create_test_org(db_session)
    proprietaire = await _make_user(db_session, org.id)
    intrus = await _make_user(db_session, org.id)
    ticket = await _make_ticket(db_session, org.id, proprietaire.id)
    service = FeedbackService(db_session)

    with pytest.raises(HTTPException) as exc:
        await service.create(intrus, _payload(ticket_id=ticket.id))

    # 404 et non 403 : répondre « interdit » confirmerait que le ticket existe.
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "TICKET_NOT_FOUND"


async def test_une_organisation_inconnue_est_refusee(db_session):
    org = await _create_test_org(db_session)
    user = await _make_user(db_session, org.id)
    service = FeedbackService(db_session)

    with pytest.raises(HTTPException) as exc:
        await service.create(user, _payload(org_id=str(uuid.uuid4())))

    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "ORGANIZATION_NOT_FOUND"


# ── Historique ────────────────────────────────────────────────────────────────

async def test_mes_retours_sont_pages_et_tries_du_plus_recent(db_session):
    org = await _create_test_org(db_session)
    user = await _make_user(db_session, org.id)
    service = FeedbackService(db_session)

    for index in range(3):
        await service.create(user, _payload(message=_message(f" ({index})")))

    premiere = await service.list_for_user(user.id, page=1, page_size=2)
    assert premiere["total"] == 3
    assert premiere["has_next"] is True
    assert len(premiere["items"]) == 2
    # Le dernier envoyé sort en tête.
    assert premiere["items"][0]["message"].endswith("(2)")

    seconde = await service.list_for_user(user.id, page=2, page_size=2)
    assert seconde["has_next"] is False
    assert len(seconde["items"]) == 1


async def test_mes_retours_ne_montrent_que_les_miens(db_session):
    org = await _create_test_org(db_session)
    moi = await _make_user(db_session, org.id)
    autre = await _make_user(db_session, org.id)
    service = FeedbackService(db_session)

    await service.create(autre, _payload(message=_message(" autre")))
    await service.create(moi, _payload(message=_message(" moi")))

    historique = await service.list_for_user(moi.id)
    assert historique["total"] == 1
    assert historique["items"][0]["message"].endswith("moi")
