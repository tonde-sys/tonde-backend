"""
Dégradation quand Redis est indisponible.

Principe : Redis est un accélérateur, pas la source de vérité. Une panne du cache
ne doit jamais transformer une lecture en erreur 500, ni faire croire à une erreur
interne là où le service est simplement indisponible.

Avant : `GET /tickets/{id}` renvoyait 500 (position calculée dans Redis sans
protection) et `POST /tickets` renvoyait 500 au lieu de 503 QUEUE_UNAVAILABLE.
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.models.user import User, UserRole
from app.schemas.ticket import CreateTicketRequest
from tests.test_ticket_service import (
    _create_test_agency,
    _create_test_org,
    _create_test_service,
)


def _client(user_id: str = "client-1") -> User:
    return User(id=user_id, role=UserRole.CLIENT, org_id=None)


@pytest.fixture
def file_disponible(mock_redis):
    """File Redis qui répond normalement (pour créer le ticket avant la panne)."""
    with patch("app.services.ticket_service.add_to_queue", new_callable=AsyncMock, return_value=1), \
         patch("app.services.ticket_service.get_queue_size", new_callable=AsyncMock, return_value=1), \
         patch("app.services.ticket_service.get_queue_position", new_callable=AsyncMock, return_value=1):
        yield


async def _un_ticket(ticket_service, db_session):
    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)
    return await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency.id, service_id=service.id), "client-1", None
    )


@pytest.mark.asyncio
async def test_lecture_d_un_ticket_survit_a_une_panne_redis(
    ticket_service, db_session, file_disponible
):
    """
    `GET /tickets/{id}` reste lisible : position inconnue (0) et total lu en base.

    C'est le comportement de l'écran public (`_waiting_count`), appliqué aussi au
    parcours client.
    """
    ticket = await _un_ticket(ticket_service, db_session)

    # La panne survient APRÈS la création : on isole le chemin de lecture.
    with patch("app.services.ticket_service.get_queue_position",
               new_callable=AsyncMock, side_effect=ConnectionError("redis down")), \
         patch("app.services.ticket_service.get_queue_size",
               new_callable=AsyncMock, side_effect=ConnectionError("redis down")):
        vue = await ticket_service.get_ticket(ticket.id, _client())

    assert vue.id == ticket.id
    assert vue.status == "waiting"
    assert vue.position == 0, "position inconnue, mais la réponse est servie"
    assert vue.total_in_queue == 1, "le total est compté en base, sans Redis"
    assert vue.estimated_wait_minutes is not None


@pytest.mark.asyncio
async def test_creation_survit_a_une_panne_du_verrou_redis(
    ticket_service, db_session
):
    """
    Le verrou de création vit dans Redis, mais l'invariant vit dans la base.

    Depuis la migration 012, « un seul ticket actif par utilisateur » est garanti
    par un index unique partiel. Le verrou n'est plus qu'une optimisation (moins
    de conflits, message plus précis) : son indisponibilité ne doit donc plus
    empêcher un client de prendre un ticket. Avant, une panne Redis produisait un
    503 alors que la base savait parfaitement se protéger seule — on refusait un
    ticket pour une raison qui n'en était plus une.
    """
    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)

    with patch("app.services.ticket_service.acquire_ticket_creation_lock",
               new_callable=AsyncMock, side_effect=ConnectionError("redis down")):
        ticket = await ticket_service.create_ticket(
            CreateTicketRequest(agency_id=agency.id, service_id=service.id), "client-1", None
        )

    assert ticket.status == "waiting"
    assert ticket.number.startswith("A-")


@pytest.mark.asyncio
async def test_la_liberation_du_verrou_ne_masque_pas_le_succes(
    ticket_service, db_session, file_disponible
):
    """
    Si la libération du verrou échoue, la création reste un succès.

    Sans cela, une erreur Redis au moment de la libération annulait une création
    pourtant committée : le client recevait une erreur alors que son ticket existait.
    """
    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)

    with patch("app.services.ticket_service.release_ticket_creation_lock",
               new_callable=AsyncMock, side_effect=ConnectionError("redis down")):
        ticket = await ticket_service.create_ticket(
            CreateTicketRequest(agency_id=agency.id, service_id=service.id), "client-1", None
        )

    assert ticket.number.endswith("001")
    assert ticket.status == "waiting"
