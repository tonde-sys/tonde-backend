"""
Un guichet ne peut pas appeler un nouveau ticket tant qu'il traite le précédent.

Défaut corrigé : `call_next` ignorait `Counter.current_ticket_id`. Un second appel
au même guichet laissait le client en cours en CALLED, sans guichet pour le servir ;
le scheduler le déclarait ABSENT 180 s plus tard alors qu'il était présent.

Ces tests n'ont pas besoin d'un vrai Redis : le contrôle a lieu avant tout accès
à la file.
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.ticket import Ticket, TicketStatus
from app.models.user import UserRole
from app.schemas.ticket import CreateTicketRequest
from tests.test_queue_atomicity import _make_counter, _make_staff
from tests.test_ticket_service import (
    _create_test_agency,
    _create_test_org,
    _create_test_service,
)


@pytest.fixture
def file_simulee(mock_redis):
    """File Redis simulée et vide : aucun échange réseau réel."""
    with patch("app.services.ticket_service.pop_next_ticket", new_callable=AsyncMock, return_value=None), \
         patch("app.services.ticket_service.add_to_queue", new_callable=AsyncMock, return_value=1), \
         patch("app.services.ticket_service.add_to_queue_if_absent", new_callable=AsyncMock, return_value=False), \
         patch("app.services.ticket_service.get_queue_size", new_callable=AsyncMock, return_value=0), \
         patch("app.services.ticket_service.get_queue_position", new_callable=AsyncMock, return_value=1), \
         patch("app.services.ticket_service.remove_from_queue", new_callable=AsyncMock), \
         patch("app.services.ticket_service.clear_deadlines", new_callable=AsyncMock), \
         patch("app.services.ticket_service.set_deadline", new_callable=AsyncMock), \
         patch("app.services.ticket_service.get_queue_snapshot", new_callable=AsyncMock, return_value=[]):
        yield


async def _guichet_occupe(db_session, ticket_service, statut: TicketStatus):
    """
    Prépare un guichet dont `current_ticket_id` pointe sur un ticket au statut donné.

    Returns:
        (org, agency, service, counter, agent, ticket)
    """
    org = await _create_test_org(db_session)
    agency = await _create_test_agency(db_session, org.id)
    service = await _create_test_service(db_session, agency.id, org.id)
    counter = await _make_counter(db_session, org.id, agency.id)
    agent = await _make_staff(
        db_session, org.id, UserRole.AGENT, counter_id=counter.id, agency_id=agency.id
    )

    created = await ticket_service.create_ticket(
        CreateTicketRequest(agency_id=agency.id, service_id=service.id), "client-1", None
    )
    row = (await db_session.execute(
        select(Ticket).where(Ticket.id == created.id).execution_options(populate_existing=True)
    )).scalar_one()
    row.status = statut
    row.counter_id = counter.id
    row.counter_name = counter.name
    counter.current_ticket_id = row.id
    await db_session.commit()
    return org, agency, service, counter, agent, created


@pytest.mark.asyncio
async def test_appeler_le_suivant_avec_un_client_called_est_refuse(
    ticket_service, db_session, file_simulee
):
    """CALLED non terminé → 409 COUNTER_BUSY, avec le numéro en cours dans l'erreur."""
    org, agency, service, counter, agent, ticket = await _guichet_occupe(
        db_session, ticket_service, TicketStatus.CALLED
    )

    with pytest.raises(HTTPException) as exc:
        await ticket_service.call_next(
            agency_id=agency.id, service_id=service.id,
            counter_id=counter.id, org_id=org.id, actor=agent,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "COUNTER_BUSY"
    assert exc.value.detail["ticket_number"] == ticket.number
    assert exc.value.detail["ticket_status"] == "called"


@pytest.mark.asyncio
async def test_appeler_le_suivant_pendant_un_service_est_refuse(
    ticket_service, db_session, file_simulee
):
    """SERVING (client au guichet) → 409 également."""
    org, agency, service, counter, agent, ticket = await _guichet_occupe(
        db_session, ticket_service, TicketStatus.SERVING
    )

    with pytest.raises(HTTPException) as exc:
        await ticket_service.call_next(
            agency_id=agency.id, service_id=service.id,
            counter_id=counter.id, org_id=org.id, actor=agent,
        )

    assert exc.value.detail["code"] == "COUNTER_BUSY"
    assert exc.value.detail["ticket_status"] == "serving"


@pytest.mark.asyncio
async def test_un_pointeur_perime_ne_bloque_pas_le_guichet(
    ticket_service, db_session, file_simulee
):
    """
    `current_ticket_id` qui pointe sur un ticket terminé ne bloque pas.

    Le statut réel est relu en base : on ne fait jamais confiance au pointeur seul,
    sinon un guichet resterait bloqué après une libération manquée.
    """
    org, agency, service, counter, agent, _ = await _guichet_occupe(
        db_session, ticket_service, TicketStatus.DONE
    )

    result = await ticket_service.call_next(
        agency_id=agency.id, service_id=service.id,
        counter_id=counter.id, org_id=org.id, actor=agent,
    )

    # La file simulée est vide : l'appel aboutit à « aucun ticket », sans 409.
    assert result["success"] is False
