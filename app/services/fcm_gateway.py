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


# ── Présentation du message ───────────────────────────────────────────────────
# FCM ne transmet pas seulement un texte : il décide de l'apparence et du
# comportement de la bannière. Sans ces champs, Android place tout dans un canal
# de repli en importance par défaut — l'appel du guichet arrive alors sans son,
# sans tête d'épingle, et l'usager ne le voit pas.

# Types qui exigent une attention immédiate. Un ticket appelé ou un
# avertissement d'absence ne se rattrapent pas : le guichet passe au suivant.
_URGENT_TYPES = frozenset({"ticket_called", "absent_warning"})

# Identifiants des canaux créés par l'application mobile
# (`LocalNotificationService`). Ils doivent correspondre au caractère près : un
# identifiant inconnu fait créer par Android un canal de repli, et l'appel au
# guichet perd son importance maximale.
CHANNEL_URGENT = "tonde_queue_urgent"
CHANNEL_PROGRESS = "tonde_queue_progress"

# Petite icône de la barre d'état. Monochrome et dédiée : l'icône de lancement,
# rendue en aplat blanc, apparaîtrait comme un carré.
SMALL_ICON = "ic_stat_tonde"

# Couleur d'accent affichée à côté de l'icône.
BRAND_COLOR = "#1E293B"

# Durée de conservation par FCM quand l'appareil est hors ligne. Un appel garde
# 5 minutes : au-delà, le ticket a été rendu et la notification induirait en
# erreur. La progression, elle, reste utile plus longtemps.
URGENT_TTL_SECONDS = 300
DEFAULT_TTL_SECONDS = 3600


def build_message(
    token: str, title: str, body: str, data: dict[str, str] | None = None
) -> dict:
    """
    Construit le message FCM v1 complet pour un appareil.

    Séparé de l'envoi pour être vérifiable sans réseau : la forme du message
    est un contrat avec l'application mobile, pas un détail d'implémentation.
    """
    payload = {k: str(v) for k, v in (data or {}).items()}
    urgent = payload.get("type") in _URGENT_TYPES
    ticket_id = payload.get("ticket_id")
    # Un fil par ticket : les notifications d'une même file se remplacent au
    # lieu de s'empiler (« encore 5 personnes », puis « 4 », puis « 3 »…).
    thread = f"ticket-{ticket_id}" if ticket_id else "tonde"

    return {
        "token": token,
        "notification": {"title": title, "body": body},
        "data": payload,
        "android": {
            # HIGH réveille l'appareil même en veille : sans cela, un téléphone
            # en Doze reçoit l'appel plusieurs minutes après le guichet.
            "priority": "HIGH",
            "ttl": f"{URGENT_TTL_SECONDS if urgent else DEFAULT_TTL_SECONDS}s",
            "notification": {
                "channel_id": CHANNEL_URGENT if urgent else CHANNEL_PROGRESS,
                "icon": SMALL_ICON,
                "color": BRAND_COLOR,
                "sound": "default",
                # Nécessaire pour que le clic soit remonté à l'application
                # lorsqu'elle est en arrière-plan.
                "click_action": "FLUTTER_NOTIFICATION_CLICK",
                "tag": thread,
                "notification_priority": (
                    "PRIORITY_MAX" if urgent else "PRIORITY_DEFAULT"
                ),
                "default_vibrate_timings": True,
            },
        },
        # iOS : même intention, autre vocabulaire. `time-sensitive` traverse le
        # mode Concentration, ce qui est exactement le besoin d'un appel.
        "apns": {
            "headers": {"apns-priority": "10" if urgent else "5"},
            "payload": {
                "aps": {
                    "sound": "default",
                    "thread-id": thread,
                    "interruption-level": (
                        "time-sensitive" if urgent else "active"
                    ),
                }
            },
        },
    }


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

    message = {"message": build_message(token, title, body, data)}
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
