"""
Modèle UserDevice — les appareils qui reçoivent les notifications d'un compte.

Pourquoi une table alors que `users.fcm_token` existait : un jeton FCM identifie
une **installation**, pas une personne. Avec une seule colonne, se connecter sur
un second téléphone écrasait le jeton du premier — ses notifications cessaient
sans aucun avertissement. C'est le défaut corrigé par B1bis.

`users.fcm_token` reste entretenu pour les clients qui ne connaissent pas encore
cet endpoint : il porte le dernier appareil enregistré.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class UserDevice(Base):
    __tablename__ = "user_devices"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )

    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Unique : un jeton ne peut appartenir qu'à un compte à la fois. Sur un
    # téléphone partagé, le second compte déplace le jeton au lieu de recevoir
    # les appels du premier.
    token: Mapped[str] = mapped_column(
        String(500), unique=True, nullable=False, index=True
    )

    platform: Mapped[str | None] = mapped_column(String(16), nullable=True)
    device_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<UserDevice {self.platform or '?'} user={self.user_id}>"
