"""
NotificationService — SMS (Africa's Talking) + FCM (Firebase v1) + In-App.

Responsabilités :
  - Envoyer des SMS via la passerelle Africa's Talking (sms_gateway)
  - Envoyer des push via Firebase Cloud Messaging API v1 (fcm_gateway)
  - Persister chaque notification dans la table 'notifications' (audit trail)
  - Exposer des méthodes de trigger liées aux événements tickets
  - Exposer la boîte de réception in-app (liste, non-lues, marquer comme lu)

Triggers :
  - notify_ticket_called()    → SMS + FCM quand le ticket est appelé
  - notify_absent_warning()   → FCM avant le passage automatique en ABSENT
  - notify_ticket_absent()    → FCM + in-app quand le client est déclaré absent
  - notify_ticket_done()      → in-app quand le service est terminé
  - notify_long_wait_alert()  → SMS si l'attente dépasse le seuil de la branch

En développement : les envois réels sont simulés (log uniquement).
Un jeton FCM que Google déclare invalide est supprimé de l'utilisateur.
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification, NotificationChannel, NotificationStatus
from app.models.ticket import Ticket
from app.models.user import User
from app.services import fcm_gateway, sms_gateway

logger = logging.getLogger(__name__)


class NotificationService:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ── Triggers métier ───────────────────────────────────────────────────────

    async def notify_ticket_called(self, ticket: Ticket, counter_name: str) -> None:
        """
        Notifie le client que son ticket est appelé (SMS + FCM).

        Args:
            ticket: Le ticket qui vient d'être appelé
            counter_name: Nom du guichet affiché au client
        """
        user = await self._get_user(ticket.user_id)
        if not user:
            return

        message = (
            f"TONDE — Votre ticket {ticket.number} est appelé "
            f"au guichet {counter_name}. Présentez-vous maintenant."
        )
        if user.phone:
            await self._send_sms(ticket.org_id, user.id, ticket.id, user.phone, message)
        if user.fcm_token:
            await self._send_fcm(
                ticket.org_id, user, ticket.id,
                title=f"Ticket {ticket.number} appelé", body=message,
                data={"type": "ticket_called", "ticket_id": ticket.id},
            )

    async def notify_absent_warning(self, ticket: Ticket, seconds_left: int) -> None:
        """
        Prévient le client appelé qu'il sera déclaré absent dans `seconds_left` secondes.
        Push uniquement (pas de SMS : le coût n'est pas justifié, l'appel SMS a déjà eu lieu).
        """
        user = await self._get_user(ticket.user_id)
        if not user or not user.fcm_token:
            return
        await self._send_fcm(
            ticket.org_id, user, ticket.id,
            title=f"Ticket {ticket.number} — présentez-vous",
            body=f"Rendez-vous au guichet {ticket.counter_name} dans {seconds_left} secondes, "
                 "sinon votre ticket sera passé.",
            data={"type": "absent_warning", "ticket_id": ticket.id},
        )

    async def notify_ticket_absent(self, ticket: Ticket) -> None:
        """Informe le client qu'il a été déclaré absent et qu'il peut revenir en file."""
        user = await self._get_user(ticket.user_id)
        if not user:
            return
        message = (
            f"TONDE — Vous avez été déclaré absent pour le ticket {ticket.number}. "
            "Vous pouvez revenir en file depuis l'application."
        )
        await self._save_notification(
            org_id=ticket.org_id, user_id=user.id, ticket_id=ticket.id,
            channel=NotificationChannel.IN_APP,
            title=f"Absent — {ticket.number}", body=message,
            status=NotificationStatus.SENT,
        )
        if user.fcm_token:
            await self._send_fcm(
                ticket.org_id, user, ticket.id,
                title=f"Ticket {ticket.number} — absent", body=message,
                data={"type": "ticket_absent", "ticket_id": ticket.id},
            )

    async def notify_ticket_done(self, ticket: Ticket) -> None:
        """Notifie le client que son service est terminé (in-app uniquement)."""
        user = await self._get_user(ticket.user_id)
        if not user:
            return
        await self._save_notification(
            org_id=ticket.org_id, user_id=user.id, ticket_id=ticket.id,
            channel=NotificationChannel.IN_APP,
            title=f"Service terminé — {ticket.number}",
            body=f"TONDE — Service terminé pour votre ticket {ticket.number}. Merci de votre visite !",
            status=NotificationStatus.SENT,
        )

    async def notify_long_wait_alert(
        self, ticket: Ticket, wait_minutes: int, threshold_minutes: int
    ) -> None:
        """
        Alerte le client si son temps d'attente dépasse le seuil configuré.
        Rules Engine DÉCISION 9 : si waiting_time > seuil → SMS d'alerte.

        Args:
            ticket: Le ticket en attente
            wait_minutes: Temps d'attente actuel en minutes
            threshold_minutes: Seuil configuré sur la branch
        """
        user = await self._get_user(ticket.user_id)
        if not user or not user.phone:
            return
        await self._send_sms(
            ticket.org_id, user.id, ticket.id, user.phone,
            f"TONDE — Votre ticket {ticket.number} attend depuis {wait_minutes} min. "
            "Vous serez appelé prochainement. Restez disponible.",
        )

    # ── Boîte de réception in-app ─────────────────────────────────────────────

    async def list_for_user(
        self, user_id: str, page: int = 1, page_size: int = 20, unread_only: bool = False
    ) -> dict:
        """
        Liste les notifications in-app d'un utilisateur (les plus récentes d'abord).

        Seules les notifications du canal IN_APP sont une « boîte de réception » : les
        SMS et les push sont des envois, ils n'ont pas à être relus dans l'application.

        Returns:
            {items, total, unread, page, page_size, has_next}
        """
        page_size = min(max(1, page_size), 100)
        base = [Notification.user_id == user_id, Notification.channel == NotificationChannel.IN_APP]
        if unread_only:
            base.append(Notification.is_read == False)  # noqa: E712

        total = (await self.db.execute(select(func.count(Notification.id)).where(*base))).scalar() or 0
        unread = (await self.db.execute(
            select(func.count(Notification.id)).where(
                Notification.user_id == user_id,
                Notification.channel == NotificationChannel.IN_APP,
                Notification.is_read == False,  # noqa: E712
            )
        )).scalar() or 0
        rows = (await self.db.execute(
            select(Notification).where(*base)
            .order_by(Notification.created_at.desc())
            .limit(page_size).offset((page - 1) * page_size)
        )).scalars().all()

        return {
            "items": [
                {
                    "id": n.id, "ticket_id": n.ticket_id, "title": n.title, "body": n.body,
                    "is_read": n.is_read, "created_at": n.created_at,
                }
                for n in rows
            ],
            "total": total,
            "unread": unread,
            "page": page,
            "page_size": page_size,
            "has_next": page * page_size < total,
        }

    async def mark_read(self, user_id: str, notification_id: str) -> bool:
        """
        Marque une notification comme lue. Retourne False si elle n'existe pas pour cet utilisateur.

        Le filtre user_id empêche de marquer la notification de quelqu'un d'autre.
        """
        result = await self.db.execute(
            update(Notification)
            .where(Notification.id == notification_id, Notification.user_id == user_id)
            .values(is_read=True)
        )
        await self.db.commit()
        return result.rowcount == 1

    async def mark_all_read(self, user_id: str) -> int:
        """Marque toutes les notifications in-app non lues comme lues. Retourne leur nombre."""
        result = await self.db.execute(
            update(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.channel == NotificationChannel.IN_APP,
                Notification.is_read == False,  # noqa: E712
            )
            .values(is_read=True)
        )
        await self.db.commit()
        return result.rowcount or 0

    # ── Canaux d'envoi ────────────────────────────────────────────────────────

    async def _send_sms(
        self, org_id: str, user_id: str, ticket_id: str | None, phone: str, body: str
    ) -> None:
        """
        Envoie un SMS via la passerelle et trace le résultat (succès ou échec).

        Ne lève jamais : une panne SMS ne doit pas bloquer l'appel d'un ticket.
        """
        try:
            await sms_gateway.send_sms(phone, body)
            await self._save_notification(
                org_id=org_id, user_id=user_id, ticket_id=ticket_id,
                channel=NotificationChannel.SMS, body=body, status=NotificationStatus.SENT,
            )
        except sms_gateway.SmsDeliveryError as exc:
            logger.error(f"[SMS] Échec envoi → {phone} : {exc}")
            await self._save_notification(
                org_id=org_id, user_id=user_id, ticket_id=ticket_id,
                channel=NotificationChannel.SMS, body=body, status=NotificationStatus.FAILED,
                error_message=str(exc)[:500],
            )

    async def _send_fcm(
        self,
        org_id: str,
        user: User,
        ticket_id: str | None,
        *,
        title: str,
        body: str,
        data: dict[str, str] | None = None,
    ) -> None:
        """
        Envoie un push via FCM v1 et trace le résultat.

        - Firebase non configuré : ignoré sans bruit (warning), aucune ligne d'audit.
        - Jeton invalide (app désinstallée) : supprimé de l'utilisateur.
        - Ne lève jamais.
        """
        try:
            await fcm_gateway.send_push(user.fcm_token, title, body, data)
            status, error = NotificationStatus.SENT, None
        except fcm_gateway.FcmNotConfigured as exc:
            logger.warning(f"[FCM] {exc} — push ignoré")
            return
        except fcm_gateway.FcmTokenInvalid as exc:
            logger.warning(f"[FCM] Jeton invalide pour user={user.id} : suppression")
            await self.db.execute(update(User).where(User.id == user.id).values(fcm_token=None))
            status, error = NotificationStatus.FAILED, str(exc)[:500]
        except fcm_gateway.FcmDeliveryError as exc:
            logger.error(f"[FCM] Échec envoi → user={user.id} : {exc}")
            status, error = NotificationStatus.FAILED, str(exc)[:500]

        await self._save_notification(
            org_id=org_id, user_id=user.id, ticket_id=ticket_id,
            channel=NotificationChannel.FCM, title=title, body=body,
            status=status, error_message=error,
        )

    # ── Helpers privés ────────────────────────────────────────────────────────

    async def _save_notification(
        self,
        org_id: str,
        user_id: str,
        ticket_id: str | None,
        channel: NotificationChannel,
        body: str,
        status: NotificationStatus,
        title: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """
        Persiste une notification dans la table 'notifications'.
        Appelé après chaque tentative d'envoi (succès ou échec).
        """
        self.db.add(Notification(
            id=str(uuid.uuid4()),
            org_id=org_id,
            user_id=user_id,
            ticket_id=ticket_id,
            channel=channel,
            title=title,
            body=body,
            status=status,
            error_message=error_message,
            sent_at=datetime.now(timezone.utc) if status == NotificationStatus.SENT else None,
        ))
        await self.db.commit()

    async def _get_user(self, user_id: str) -> User | None:
        """Récupère l'utilisateur pour accéder à son phone et fcm_token."""
        result = await self.db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()
