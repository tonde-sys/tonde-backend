"""
Passerelle email — SMTP sortant, point d'envoi UNIQUE du projet.

Même contrat que `sms_gateway` :
  - en développement, aucun appel réseau : l'email est seulement journalisé ;
  - en production, un SMTP réel est requis, et l'absence de configuration est
    une ERREUR explicite, pas un silence.

Pourquoi `smtplib` (stdlib) et pas `aiosmtplib` : l'envoi SMTP est bloquant,
mais il part dans un thread (`asyncio.to_thread`) et le volume est de quelques
dizaines d'emails par jour. Ajouter une dépendance pour cela n'apporterait rien
qu'un CVE à suivre.

Ce que cette passerelle N'EST PAS : un système de campagnes. Pas de pièce
jointe, pas de template HTML riche, pas de file d'attente avec retries. Un
email transactionnel (réinitialisation de mot de passe, rapport quotidien),
texte brut, qui arrive ou qui échoue bruyamment.
"""
import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.core.config import settings

logger = logging.getLogger(__name__)


class EmailDeliveryError(Exception):
    """L'email n'a pas pu être remis au serveur SMTP (réseau, auth, destinataire refusé…)."""


def is_configured() -> bool:
    """Un SMTP complet est-il configuré ? (sert au rapport de santé et au CLI)"""
    return bool(settings.SMTP_HOST and settings.SMTP_FROM)


def _build_message(to: str, subject: str, body: str, reply_to: str | None = None) -> EmailMessage:
    message = EmailMessage()
    message["From"] = settings.SMTP_FROM
    message["To"] = to
    message["Subject"] = subject
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(body)
    return message


def _send_sync(to: str, subject: str, body: str, reply_to: str | None = None) -> None:
    """
    Envoi SMTP bloquant — TOUJOURS appelé depuis un thread.

    Deux modes de chiffrement selon le port :
      - 465 : TLS implicite dès la connexion (SMTP_SSL) ;
      - autre (587, 25) : connexion claire puis STARTTLS si SMTP_USE_TLS.
    Se tromper de mode est la première cause d'échec d'envoi : d'où le choix
    explicite sur le port plutôt qu'un simple booléen.
    """
    message = _build_message(to, subject, body, reply_to)
    host, port = settings.SMTP_HOST, settings.SMTP_PORT
    context = ssl.create_default_context()

    if port == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(
            host, port, timeout=settings.SMTP_TIMEOUT_SECONDS, context=context
        )
    else:
        server = smtplib.SMTP(host, port, timeout=settings.SMTP_TIMEOUT_SECONDS)

    try:
        server.ehlo()
        if port != 465 and settings.SMTP_USE_TLS:
            server.starttls(context=context)
            server.ehlo()
        if settings.SMTP_USER:
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001 — la fermeture ne doit pas masquer l'erreur d'envoi
            server.close()


async def send_email(to: str, subject: str, body: str, reply_to: str | None = None) -> None:
    """
    Envoie un email transactionnel.

    Args:
        to: Destinataire
        subject: Objet (pas de retour à la ligne : c'est un en-tête)
        body: Corps en texte brut
        reply_to: Adresse de réponse (défaut : aucune)

    Raises:
        EmailDeliveryError: Échec d'envoi — l'appelant décide de bloquer ou non
    """
    if settings.is_development:
        # Journalisé en entier : en dev, l'email EST le canal de test.
        logger.info(f"[EMAIL DEV] → {to} | {subject}\n{body}")
        return

    if not is_configured():
        raise EmailDeliveryError("SMTP non configuré (SMTP_HOST / SMTP_FROM)")

    import asyncio

    try:
        await asyncio.to_thread(_send_sync, to, subject, body, reply_to)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise EmailDeliveryError(f"Envoi SMTP échoué : {exc}") from exc

    logger.info(f"[EMAIL] Envoyé → {to} | {subject}")


async def send_password_reset_email(to: str, name: str, link: str, ttl_minutes: int) -> None:
    """Compose et envoie l'email de réinitialisation de mot de passe."""
    body = (
        f"Bonjour {name},\n\n"
        "Une réinitialisation de mot de passe a été demandée pour votre compte TONDE.\n\n"
        f"Ouvrez ce lien pour choisir un nouveau mot de passe :\n{link}\n\n"
        f"Ce lien expire dans {ttl_minutes} minutes et ne peut servir qu'une fois.\n\n"
        "Si vous n'êtes pas à l'origine de cette demande, ignorez cet email : "
        "votre mot de passe actuel reste valable.\n\n"
        f"— L'équipe TONDE\n{settings.SUPPORT_EMAIL}\n"
    )
    await send_email(
        to,
        "Réinitialisation de votre mot de passe TONDE",
        body,
        reply_to=settings.SUPPORT_EMAIL,
    )
