"""
Tests unitaires — QueueWebSocketManager

Note : Les tests du Redis Pub/Sub listener sont exclus car ils nécessitent
une infrastructure de test asyncio spécifique (Event + timeout) qui peut
varier selon la version de pytest-asyncio. Ces tests seront ajoutés dans
une PR dédiée avec la bonne configuration.

Les tests actuels couvrent les méthodes synchrones et utilitaires du manager.
"""
import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch

from app.websocket.queue_ws import QueueWebSocketManager
from app.websocket.events import EventType


# ── Tests méthodes utilitaires ───────────────────────────────────────────────

def test_ws_manager_initializes_empty():
    """Le manager démarre avec des collections vides."""
    manager = QueueWebSocketManager()
    assert manager.connections_by_ticket == {}
    assert manager.connections_by_agency == {}
    assert manager.counter_connections == {}


def test_get_stats_empty():
    """get_stats() retourne 0 pour un manager sans connexions."""
    manager = QueueWebSocketManager()
    stats = manager.get_stats()
    assert stats["clients_by_ticket"] == 0
    assert stats["agencies_tracked"] == 0
    assert stats["counter_connections"] == 0
    assert stats["total_clients"] == 0


def test_disconnect_client_removes_from_ticket_index():
    """disconnect_client() retire la connexion de connections_by_ticket."""
    manager = QueueWebSocketManager()
    mock_ws = MagicMock()
    mock_ws.client_state.name = "CONNECTED"

    manager.connections_by_ticket["ticket-1"] = {mock_ws}
    manager.connections_by_agency["agency-1"] = {mock_ws}

    manager.disconnect_client("ticket-1", "agency-1")

    assert "ticket-1" not in manager.connections_by_ticket


@pytest.mark.asyncio
async def test_send_to_ticket_no_connection_does_nothing():
    """send_to_ticket() sur un ticket sans connexion ne lève pas d'exception."""
    manager = QueueWebSocketManager()
    # Ne doit pas lever d'exception
    await manager.send_to_ticket("ticket-inexistant", {"type": "test"})


@pytest.mark.asyncio
async def test_broadcast_to_agency_no_connections_does_nothing():
    """broadcast_to_agency() sans connexions ne lève pas d'exception."""
    manager = QueueWebSocketManager()
    await manager.broadcast_to_agency("agency-inexistante", {"type": "test"})


@pytest.mark.asyncio
async def test_send_to_counter_no_connection_does_nothing():
    """send_to_counter() sans connexion ne lève pas d'exception."""
    manager = QueueWebSocketManager()
    await manager.send_to_counter("counter-inexistant", {"type": "test"})


@pytest.mark.asyncio
async def test_send_to_ticket_cleans_dead_connection():
    """send_to_ticket() nettoie automatiquement une connexion morte."""
    manager = QueueWebSocketManager()
    dead_ws = AsyncMock()
    dead_ws.send_text = AsyncMock(side_effect=Exception("connexion fermée"))
    manager.connections_by_ticket["ticket-dead"] = {dead_ws}

    # Ne lève pas d'exception
    await manager.send_to_ticket("ticket-dead", {"type": "test"})

    # La connexion morte est retirée
    assert "ticket-dead" not in manager.connections_by_ticket


@pytest.mark.asyncio
async def test_publish_event_calls_redis_publish():
    """publish_event() publie dans le bon canal Redis."""
    manager = QueueWebSocketManager()
    mock_redis = AsyncMock()

    with patch("app.core.redis.get_redis", return_value=mock_redis):
        await manager.publish_event("org-123", {"type": "your_turn", "ticket_id": "t-1"})

    mock_redis.publish.assert_called_once()
    call_args = mock_redis.publish.call_args
    channel = call_args.args[0]
    payload = json.loads(call_args.args[1])

    assert "org-123" in channel
    assert payload["type"] == "your_turn"
    assert payload["ticket_id"] == "t-1"


@pytest.mark.asyncio
async def test_dispatch_event_routes_your_turn():
    """_dispatch_event() route YOUR_TURN vers send_to_ticket()."""
    manager = QueueWebSocketManager()

    with patch.object(manager, "send_to_ticket", new_callable=AsyncMock) as mock_send:
        await manager._dispatch_event({
            "type": EventType.YOUR_TURN,
            "ticket_id": "ticket-123",
            "ticket_number": "A-5",
            "counter_name": "Guichet 1",
        })

    mock_send.assert_called_once_with("ticket-123", {
        "type": EventType.YOUR_TURN,
        "ticket_id": "ticket-123",
        "ticket_number": "A-5",
        "counter_name": "Guichet 1",
    })


@pytest.mark.asyncio
async def test_dispatch_event_routes_ticket_called():
    """_dispatch_event() route TICKET_CALLED vers broadcast_to_agency()."""
    manager = QueueWebSocketManager()

    with patch.object(manager, "broadcast_to_agency", new_callable=AsyncMock) as mock_broadcast:
        await manager._dispatch_event({
            "type": EventType.TICKET_CALLED,
            "called_number": "A-5",
            "agency_id": "agency-xyz",
        })

    mock_broadcast.assert_called_once_with("agency-xyz", {
        "type": EventType.TICKET_CALLED,
        "called_number": "A-5",
        "agency_id": "agency-xyz",
    })


@pytest.mark.asyncio
async def test_dispatch_event_unknown_type_does_nothing():
    """_dispatch_event() avec type inconnu ne lève pas d'exception."""
    manager = QueueWebSocketManager()
    # Ne doit pas lever d'exception
    await manager._dispatch_event({"type": "type_inconnu_xyz"})
