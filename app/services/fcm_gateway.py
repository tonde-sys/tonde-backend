"""
Passerelle Firebase Cloud Messaging — API HTTP v1.

L'ancienne API « legacy » (`fcm.googleapis.com/fcm/send` + clé serveur) a été arrêtée
par Google : elle ne fonctionne plus. L'API v1 s'authentifie avec un jeton OAuth2
obtenu à partir d'un compte de service Firebase :

  1. on signe un JWT (RS256) avec la clé privée du compte de service ;
  2. on l'échange contre un jeton d'accès chez Google (valable ~1 h, mis en cache) ;
  3. on envoie le message à /v1/projects/{projet}/messages:send.

Configuration : FIREBASE_PROJECT_ID + FIREBASE_SERVICE_ACCOUNT_JSON (contenu JSON ou
chemin du fichier). En développement : aucun appel réseau, le push est journalisé.
"""
import json
import logging
import time
from pathlib import Path

import httpx
from jose import jwt

from app.core.config import settings

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_TIMEOUT_SECONDS = 10.0
_TOKEN_REFRESH_MARGIN_SECONDS = 120

# Jeton d'accès en cache : (jeton, timestamp d'expiration)
_cached_token: tuple[str, float] | None = None


class FcmNotConfigured(Exception):
    """Firebase n'est pas configuré (projet ou compte de service manquant)."""


class FcmDeliveryError(Exception):
    """L'envoi du push a échoué (réseau, authentification, message refusé)."""


class FcmTokenInvalid(FcmDeliveryError):
    """Le jeton de l'appareil n'est plus valide (app désinstallée) : à supprimer côté serveur."""


def _load_service_account() -> dict:
    """
    Charge le compte de service depuis FIREBASE_SERVICE_ACCOUNT_JSON.

    La valeur peut être le JSON lui-même ou le chemin d'un fichier JSON.

    Raises:
        FcmNotConfigured: Variable absente ou contenu illisible
    """
    raw = settings.FIREBASE_SERVICE_ACCOUNT_JSON.strip()
    if not raw or not settings.FIREBASE_PROJECT_ID:
        raise FcmNotConfigured("FIREBASE_PROJECT_ID / FIREBASE_SERVICE_ACCOUNT_JSON non configurés")
    try:
        content = raw if raw.startswith("{") else Path(raw).read_text(encoding="utf-8")
        account = json.loads(content)
        if "client_email" not in account or "private_key" not in account:
            raise ValueError("client_email / private_key manquants")
        return account
    except (OSError, ValueError) as exc:
        raise FcmNotConfigured(f"Compte de service Firebase illisible : {exc}") from exc


async def _access_token(client: httpx.AsyncClient) -> str:
    """Retourne un jeton OAuth2 valide (en cache jusqu'à ~2 min avant expiration)."""
    global _cached_token
    now = time.time()
    if _cached_token and _cached_token[1] - _TOKEN_REFRESH_MARGIN_SECONDS > now:
        return _cached_token[0]

    account = _load_service_account()
    assertion = jwt.encode(
        {
            "iss": account["client_email"],
            "scope": _SCOPE,
            "aud": _TOKEN_URL,
            "iat": int(now),
            "exp": int(now) + 3600,
        },
        account["private_key"],
        algorithm="RS256",
    )
    response = await client.post(
        _TOKEN_URL,
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
    )
    response.raise_for_status()
    body = response.json()
    _cached_token = (body["access_token"], now + int(body.get("expires_in", 3600)))
    return _cached_token[0]


def reset_token_cache() -> None:
    """Vide le jeton en cache (utilisé par les tests)."""
    global _cached_token
    _cached_token = None


async def send_push(token: str, title: str, body: str, data: dict[str, str] | None = None) -> None:
    """
    Envoie une notification push à un appareil.

    Args:
        token: Jeton FCM de l'appareil
        title: Titre de la notification
        body: Texte de la notification
        data: Données transmises à l'application (valeurs obligatoirement en chaînes)

    Raises:
        FcmNotConfigured: Firebase non configuré (hors développement)
        FcmTokenInvalid: Jeton d'appareil expiré ou inconnu
        FcmDeliveryError: Autre échec d'envoi
    """
    if settings.is_development:
        logger.info(f"[FCM DEV] → {token[:20]}... title={title}")
        return

    message = {
        "message": {
            "token": token,
            "notification": {"title": title, "body": body},
            "data": {k: str(v) for k, v in (data or {}).items()},
            "android": {"priority": "HIGH"},
        }
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            access_token = await _access_token(client)
            response = await client.post(
                f"https://fcm.googleapis.com/v1/projects/{settings.FIREBASE_PROJECT_ID}/messages:send",
                headers={"Authorization": f"Bearer {access_token}"},
                json=message,
            )
    except FcmNotConfigured:
        raise
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise FcmDeliveryError(f"Échec d'appel FCM : {exc}") from exc

    if response.status_code == 200:
        return
    # 404 UNREGISTERED / 400 INVALID_ARGUMENT sur le jeton : l'appareil n'existe plus
    if response.status_code in (400, 404) and any(
        marker in response.text for marker in ("UNREGISTERED", "INVALID_ARGUMENT", "NOT_FOUND")
    ):
        raise FcmTokenInvalid(f"Jeton FCM invalide : {response.text[:200]}")
    raise FcmDeliveryError(f"FCM a répondu {response.status_code} : {response.text[:200]}")
