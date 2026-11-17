"""HTTP Idempotency-Key alias; no database writes."""
import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock, patch
from app.routers.tickets import create_ticket
from app.schemas.ticket import CreateTicketRequest
from app.models.user import User


@pytest.mark.asyncio
async def test_header_and_body_alias():
    body = CreateTicketRequest(agency_id='a', service_id='s')
    user = User(id='u', org_id=None)
    with patch('app.routers.tickets.TicketService') as service:
        service.return_value.create_ticket = AsyncMock(return_value={'ok': True})
        assert await create_ticket(body, idempotency_key='abcdefgh', current_user=user, db=None) == {'ok': True}
        assert service.return_value.create_ticket.call_args.args[0].client_action_id == 'abcdefgh'
        await create_ticket(body.model_copy(update={'client_action_id':'abcdefgh'}),
                            idempotency_key='abcdefgh', current_user=user, db=None)
        with pytest.raises(HTTPException) as mismatch:
            await create_ticket(body.model_copy(update={'client_action_id':'ijklmnop'}),
                                idempotency_key='abcdefgh', current_user=user, db=None)
        assert mismatch.value.detail['code'] == 'IDEMPOTENCY_KEY_MISMATCH'
