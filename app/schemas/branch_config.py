"""
Schémas Pydantic v2 pour la configuration de la file d'attente d'une Branch.

S2-10 — Agency Config.
"""
import re
from typing import Any
from pydantic import BaseModel, ConfigDict, field_validator


# ── Sous-schéma horaires ──────────────────────────────────────────────────────

def validate_hhmm(value: object, label: str = "horaire") -> str:
    """
    Vérifie qu'une valeur est une heure HH:MM RÉELLE (00:00 à 23:59).

    L'ancien contrôle (`\\d{2}:\\d{2}`) acceptait « 25:99 », et une valeur non textuelle
    provoquait une erreur 500. Les horaires sont comparés tels quels (chaînes zéro-complétées)
    pour décider si une branch est ouverte : ils doivent donc être strictement valides.

    Raises:
        ValueError: format invalide ou heure inexistante
    """
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        raise ValueError(f"Format horaire invalide pour {label} : utiliser HH:MM")
    hour, minute = int(value[:2]), int(value[3:])
    if hour > 23 or minute > 59:
        raise ValueError(f"Heure inexistante pour {label} : {value}")
    return value


class DayHours(BaseModel):
    """Horaires d'ouverture pour un jour de la semaine."""
    open: str   # ex: "08:00"
    close: str  # ex: "17:00"

    @field_validator("open", "close")
    @classmethod
    def valid_time_format(cls, v: str) -> str:
        return validate_hhmm(v)


# Jours valides
VALID_DAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


# ── Requête de mise à jour (PATCH) ────────────────────────────────────────────

class UpdateBranchConfigRequest(BaseModel):
    """
    Tous les champs sont optionnels — PATCH partiel.
    Seuls les champs fournis sont mis à jour.
    """
    max_daily_tickets: int | None = None
    avg_service_minutes: int | None = None
    max_wait_minutes_alert: int | None = None
    operating_hours: dict[str, Any] | None = None
    enable_sms_reminders: bool | None = None
    reminder_interval_minutes: int | None = None
    supported_languages: list[str] | None = None

    @field_validator("max_daily_tickets")
    @classmethod
    def max_tickets_positive(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("max_daily_tickets doit être >= 0 (0 = illimité)")
        return v

    @field_validator("avg_service_minutes")
    @classmethod
    def avg_service_positive(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("avg_service_minutes doit être >= 1")
        return v

    @field_validator("max_wait_minutes_alert")
    @classmethod
    def alert_threshold_positive(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("max_wait_minutes_alert doit être >= 1")
        return v

    @field_validator("reminder_interval_minutes")
    @classmethod
    def reminder_interval_positive(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("reminder_interval_minutes doit être >= 1")
        return v

    @field_validator("operating_hours")
    @classmethod
    def valid_operating_hours(cls, v: dict | None) -> dict | None:
        """Vérifie que les jours sont valides et que les horaires sont au bon format."""
        if v is None:
            return v
        for day, hours in v.items():
            if day not in VALID_DAYS:
                raise ValueError(
                    f"Jour invalide '{day}'. Valeurs acceptées : {sorted(VALID_DAYS)}"
                )
            if not isinstance(hours, dict) or "open" not in hours or "close" not in hours:
                raise ValueError(
                    f"Horaires invalides pour '{day}'. Format attendu : {{\"open\": \"HH:MM\", \"close\": \"HH:MM\"}}"
                )
            # Heures réelles (pas « 25:99 ») et ouverture strictement avant fermeture
            for key in ("open", "close"):
                validate_hhmm(hours[key], f"{day}.{key}")
            if hours["open"] >= hours["close"]:
                raise ValueError(f"{day} : l'ouverture ({hours['open']}) doit précéder la fermeture ({hours['close']})")
        return v

    @field_validator("supported_languages")
    @classmethod
    def valid_languages(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and len(v) == 0:
            raise ValueError("supported_languages ne peut pas être une liste vide")
        return v


# ── Réponse (GET + PATCH) ─────────────────────────────────────────────────────

class BranchConfigResponse(BaseModel):
    """Configuration complète de la file d'attente d'une branch."""
    model_config = ConfigDict(from_attributes=True)

    branch_id: str
    # ── Capacité ──────────────────────────────────────────────
    max_daily_tickets: int
    avg_service_minutes: int
    # ── Alertes ───────────────────────────────────────────────
    max_wait_minutes_alert: int
    # ── Horaires ──────────────────────────────────────────────
    opens_at: str                           # Horaire global (fallback)
    closes_at: str                          # Horaire global (fallback)
    operating_hours: dict[str, Any] | None  # Horaires détaillés par jour
    # ── Notifications ─────────────────────────────────────────
    enable_sms_reminders: bool
    reminder_interval_minutes: int
    # ── Localisation ──────────────────────────────────────────
    supported_languages: list[str]
