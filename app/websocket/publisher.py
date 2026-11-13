"""
Publication d'événements temps réel — appelée par les services métier.

Règle d'architecture : le Queue Engine PUBLIE, le WebSocket Manager DIFFUSE.
Les services ne touchent jamais une connexion WebSocket : ils publient dans
Redis Pub/Sub, et chaque instance de l'API relaie à ses propres clients.

Une panne temps réel ne doit JAMAIS faire échouer une opération métier :
un ticket appelé reste appelé même si la notification temps réel échoue
(le client récupère son état au prochain GET ou à la reconnexion).
"""
import logging
from typing import Any

from app.websocket.events import EventType

logger = logging.getLogger(__name__)


async def publish_event(org_id: str | None, event: dict[str, Any]) -> bool:
    """
    Publie un événement sur le canal de l'organisation. Ne lève jamais.

    Args:
        org_id: Organisation (canal tonde:events:{org_id}); "public" si absente
        event: Dict contenant au minimum 'type'

    Returns:
        True si publié, False si Redis a échoué (erreur journalisée).
    """
    from app.websocket.queue_ws import ws_manager

    try:
        await ws_manager.publish_event(org_id or "public", event)
        return True
    except Exception as exc:  # noqa: BLE001 — jamais bloquant pour le métier
        logger.error(f"Publication temps réel échouée ({event.get('type')}): {exc}")
        return False


def queue_snapshot_event(
    *,
    agency_id: str,
    service_id: str,
    positions: dict[str, int],
    current_called_number: str | None,
    eta_seconds_per_position: int,
) -> dict[str, Any]:
    """
    Construit l'événement interne décrivant l'état complet d'une file.

    Args:
        agency_id: Branch concernée
        service_id: Service concerné
        positions: {ticket_id: position (1 = prochain)} pour tous les tickets en attente
        current_called_number: Dernier numéro appelé dans ce service
        eta_seconds_per_position: Durée moyenne d'un service (secondes), pour l'ETA
    """
    return {
        "type": EventType.QUEUE_SNAPSHOT.value,
        "agency_id": agency_id,
        "service_id": service_id,
        "positions": positions,
        "total_in_queue": len(positions),
        "current_called_number": current_called_number or "",
        "eta_seconds_per_position": eta_seconds_per_position,
    }
