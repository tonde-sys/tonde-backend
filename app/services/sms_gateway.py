"""
Passerelle SMS — Africa's Talking (Burundi, RDC, Afrique de l'Est).

Point d'envoi UNIQUE pour tout le projet (OTP et notifications). Utilise httpx en
async : l'ancien SDK `africastalking` est synchrone et bloquait la boucle
d'événements pendant tout l'appel réseau.

En développement : aucun appel réseau, le SMS est seulement journalisé.
"""
import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_PRODUCTION_URL = "https://api.africastalking.com/version1/messaging"
_SANDBOX_URL = "https://api.sandbox.africastalking.com/version1/messaging"
_TIMEOUT_SECONDS = 10.0

# Codes de statut Africa's Talking signifiant « accepté par l'opérateur »
_ACCEPTED_STATUS_CODES = {100, 101, 102}


class SmsDeliveryError(Exception):
    """Le SMS n'a pas pu être remis à la passerelle (réseau, clé invalide, numéro rejeté…)."""


def _endpoint() -> str:
    """Le bac à sable Africa's Talking a sa propre URL (username « sandbox »)."""
    return _SANDBOX_URL if settings.AFRICAS_TALKING_USERNAME == "sandbox" else _PRODUCTION_URL


async def send_sms(phone: str, message: str) -> None:
    """
    Envoie un SMS.

    Args:
        phone: Numéro au format international (+25712345678)
        message: Corps du SMS (160 caractères recommandés)

    Raises:
        SmsDeliveryError: Échec d'envoi (l'appelant décide de bloquer ou non)
    """
    if settings.is_development:
        logger.info(f"[SMS DEV] → {phone} : {message}")
        return

    if not settings.AFRICAS_TALKING_API_KEY:
        raise SmsDeliveryError("AFRICAS_TALKING_API_KEY non configurée")

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                _endpoint(),
                headers={
                    "apiKey": settings.AFRICAS_TALKING_API_KEY,
                    "Accept": "application/json",
                },
                data={
                    "username": settings.AFRICAS_TALKING_USERNAME,
                    "to": phone,
                    "message": message,
                    "from": settings.AFRICAS_TALKING_SENDER_ID,
                },
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        raise SmsDeliveryError(f"Passerelle SMS injoignable ou refus HTTP : {exc}") from exc
    except ValueError as exc:  # réponse non JSON
        raise SmsDeliveryError("Réponse invalide de la passerelle SMS") from exc

    recipients = payload.get("SMSMessageData", {}).get("Recipients", [])
    if not recipients:
        raise SmsDeliveryError(
            f"Aucun destinataire accepté : {payload.get('SMSMessageData', {}).get('Message', 'inconnu')}"
        )
    for recipient in recipients:
        if recipient.get("statusCode") not in _ACCEPTED_STATUS_CODES:
            raise SmsDeliveryError(
                f"SMS refusé pour {recipient.get('number')} : {recipient.get('status', 'inconnu')}"
            )
    logger.info(f"[SMS] Envoyé → {phone}")
