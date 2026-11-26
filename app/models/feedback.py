"""
Modèle Feedback — Table 'feedback' dans PostgreSQL.

Un retour envoyé depuis l'application mobile : avis sur un passage, signalement
de bug, ou suggestion. C'est le seul canal par lequel un client peut dire ce qui
n'a pas fonctionné sans passer par le téléphone du support.

Choix de conception :

  - `org_id` et `ticket_id` sont NULLABLES. Un avis peut porter sur une
    organisation précise (« l'accueil de l'agence X était lent ») ou sur
    l'application elle-même (« le bouton plante »), auquel cas il n'y a ni
    organisation ni ticket à rattacher.
  - `ondelete="SET NULL"` et non `CASCADE` : un retour déjà envoyé ne doit pas
    disparaître parce qu'un ticket a été purgé ou qu'une organisation a fermé
    son compte. C'est une trace de ce que l'utilisateur a vécu, pas une donnée
    dérivée du ticket.
  - `user_id` en `CASCADE` : si le compte est supprimé (droit à l'effacement),
    ses retours partent avec lui.
  - Les champs techniques (`app_version`, `platform`, `device_model`, `locale`)
    sont remplis par l'application au moment de l'envoi. Ils évitent d'aller
    demander au client « quelle version avez-vous ? » — la première question
    que le support pose, et celle à laquelle personne ne sait répondre.
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class FeedbackCategory(str, enum.Enum):
    """Ce que l'utilisateur veut dire.

    `review`   — avis sur un passage (note 1 à 5 attendue)
    `bug`      — quelque chose ne fonctionne pas
    `suggestion` — idée d'amélioration
    """
    REVIEW     = "review"
    BUG        = "bug"
    SUGGESTION = "suggestion"


class FeedbackStatus(str, enum.Enum):
    """Où en est le traitement, du point de vue du support."""
    RECEIVED = "received"   # arrivé, personne ne l'a encore ouvert
    READ     = "read"       # lu par le support
    RESOLVED = "resolved"   # traité (corrigé, répondu, ou classé sans suite)


class Feedback(Base):
    __tablename__ = "feedback"

    __table_args__ = (
        # « Mes retours » (les plus récents d'abord) et le comptage du quota
        # horaire lisent tous deux (user_id, created_at) : un seul index sert
        # les deux, plutôt que de faire un tri en mémoire.
        Index("ix_feedback_user_created", "user_id", "created_at"),
        # Les contraintes de plage de note et de longueur de message vivent
        # uniquement dans la migration 021 : elles s'appuient sur `char_length`
        # et `btrim`, qui n'existent pas en SQLite. Les inscrire ici casserait
        # la création des tables dans les tests, pour un gain nul — l'API valide
        # déjà ces deux règles, et la migration protège la base réelle.
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True,
    )
    org_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    ticket_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tickets.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    category: Mapped[FeedbackCategory] = mapped_column(
        SAEnum(FeedbackCategory, name="feedbackcategory"), index=True,
    )
    # Note de 1 à 5. Facultative : un signalement de bug n'a pas à être noté,
    # et forcer une note ferait abandonner la moitié des envois.
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text)
    app_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(16), nullable=True)
    device_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    locale: Mapped[str | None] = mapped_column(String(8), nullable=True)
    status: Mapped[FeedbackStatus] = mapped_column(
        SAEnum(FeedbackStatus, name="feedbackstatus"),
        default=FeedbackStatus.RECEIVED, index=True,
    )
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    def __repr__(self) -> str:
        return f"<Feedback {self.category} user={self.user_id} [{self.status}]>"
