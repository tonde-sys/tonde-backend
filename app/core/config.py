"""
Configuration centrale de TONDE Backend.
Toutes les variables d'environnement sont lues et validées ici.
Pydantic-settings garantit qu'une variable manquante fait crasher
l'app au démarrage plutôt qu'en cours d'exécution.

En production, la configuration est en plus AUDITÉE au démarrage : une valeur
dangereuse (secret JWT par défaut, DEBUG actif, tables auto-créées…) empêche
l'application de démarrer au lieu de tourner silencieusement en mode vulnérable.
"""
import logging
from typing import List

from pydantic import ConfigDict, field_validator, model_validator
from pydantic_settings import BaseSettings

VALID_ENVIRONMENTS = ("development", "staging", "production")

# Valeurs de JWT_SECRET_KEY qui ne doivent JAMAIS tourner hors développement
_INSECURE_JWT_SECRETS = {
    "", "x", "secret", "changeme", "change_this_in_production",
    "ci-test-secret-key-not-for-production",
}


class Settings(BaseSettings):
    # ── App ───────────────────────────────────────────────────
    APP_NAME: str = "Tonde API"
    APP_VERSION: str = "1.0.0"
    APP_PORT: int = 8000
    # Valeurs : development | staging | production
    ENVIRONMENT: str = "development"
    DEBUG: bool = True

    # ── Base de données ───────────────────────────────────────
    DATABASE_URL: str

    # ── Migrations ────────────────────────────────────────────
    # True uniquement en développement pour créer les tables sans Alembic.
    # En production, toujours False — utiliser 'alembic upgrade head'.
    CREATE_TABLES_ON_STARTUP: bool = False

    # ── Redis ─────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379"

    # ── JWT ───────────────────────────────────────────────────
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # ── OTP / SMS ─────────────────────────────────────────────
    AFRICAS_TALKING_USERNAME: str = "sandbox"
    AFRICAS_TALKING_API_KEY: str = ""
    AFRICAS_TALKING_SENDER_ID: str = "TONDE"
    OTP_EXPIRE_MINUTES: int = 5
    OTP_MAX_ATTEMPTS: int = 3

    # ── Firebase Cloud Messaging (API HTTP v1) ────────────────
    # L'ancienne API « legacy » (clé serveur) est arrêtée par Google.
    # FIREBASE_SERVICE_ACCOUNT_JSON : contenu JSON du compte de service,
    # OU chemin vers le fichier JSON.
    FIREBASE_PROJECT_ID: str = ""
    FIREBASE_SERVICE_ACCOUNT_JSON: str = ""

    # ── Queue Engine : délais ─────────────────────────────────
    # Délai laissé au client appelé pour se présenter avant d'être ABSENT.
    ABSENT_TIMEOUT_SECONDS: int = 180
    # Avertissement « dépêchez-vous » envoyé avant l'expiration.
    ABSENT_WARNING_SECONDS: int = 120
    # Un client ABSENT peut revenir en file pendant cette fenêtre, ensuite son ticket est clos.
    ABSENT_RETURN_WINDOW_MINUTES: int = 30
    # Un ticket jamais appelé est clos après ce délai (évite de bloquer le client).
    WAITING_TICKET_TTL_HOURS: int = 12
    # Un ticket SERVING oublié par l'agent passe INCOMPLETE après ce délai.
    SERVING_TICKET_TTL_HOURS: int = 6
    # Un ticket INCOMPLETE non résolu est clos après ce délai.
    INCOMPLETE_TICKET_TTL_HOURS: int = 24

    # ── Scheduler (tâches de fond) ────────────────────────────
    SCHEDULER_ENABLED: bool = True
    SCHEDULER_INTERVAL_SECONDS: int = 5
    MAINTENANCE_INTERVAL_SECONDS: int = 60

    # ── Offline sync / QR / exports ───────────────────────────
    SYNC_WINDOW_DAYS: int = 7
    SYNC_MAX_ACTIONS: int = 50
    QR_TICKET_TTL_HOURS: int = 24
    EXPORT_MAX_ROWS: int = 100_000
    EXPORT_BATCH_SIZE: int = 1_000

    # ── WebSocket ─────────────────────────────────────────────
    # Connexions WebSocket max. par IP et par minute (DÉCISION 6 : 10). 0 = désactivé.
    # Attention : une salle d'attente dont tous les clients partagent le Wi-Fi (même IP
    # publique) peut atteindre cette limite — à relever si besoin.
    WS_CONNECT_LIMIT_PER_MINUTE: int = 10

    # ── CORS ──────────────────────────────────────────────────
    ALLOWED_ORIGINS: str = "http://localhost:3000,http://localhost:5173"

    # ── Propriétés dérivées ───────────────────────────────────
    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        """
        Accepte les URLs fournies par les plateformes managées.

        Render (et Heroku) injectent ``postgres://`` ou ``postgresql://`` ;
        SQLAlchemy async a besoin du driver explicite ``postgresql+asyncpg://``.
        Une URL déjà qualifiée est laissée intacte.
        """
        if not isinstance(value, str):
            return value
        url = value.strip()
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        if url.startswith("postgresql://"):
            url = "postgresql+asyncpg://" + url[len("postgresql://"):]
        return url

    @property
    def is_development(self) -> bool:
        """True en développement (OTP de test 123456, SMS simulés)."""
        return self.ENVIRONMENT == "development"

    @property
    def is_production(self) -> bool:
        """True en production (audit de configuration strict, docs désactivées)."""
        return self.ENVIRONMENT == "production"

    def get_allowed_origins(self) -> List[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    def configure_logging(self) -> None:
        """Configure le logging structuré selon l'environnement."""
        level = logging.DEBUG if self.DEBUG else logging.INFO
        logging.basicConfig(
            level=level,
            format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    # ── Audit de configuration ────────────────────────────────
    @model_validator(mode="after")
    def _audit_configuration(self) -> "Settings":
        """
        Refuse de démarrer avec une configuration dangereuse.

        - ENVIRONMENT doit être development, staging ou production.
        - Hors développement : secret JWT fort (≥ 32 caractères, pas une valeur connue).
        - En production : DEBUG désactivé et création auto des tables interdite.
        """
        if self.ENVIRONMENT not in VALID_ENVIRONMENTS:
            raise ValueError(
                f"ENVIRONMENT invalide : '{self.ENVIRONMENT}'. "
                f"Valeurs acceptées : {', '.join(VALID_ENVIRONMENTS)}"
            )

        if self.ENVIRONMENT != "development":
            secret = self.JWT_SECRET_KEY
            if len(secret) < 32 or secret.lower() in _INSECURE_JWT_SECRETS:
                raise ValueError(
                    "JWT_SECRET_KEY trop faible pour cet environnement : "
                    "utilisez au moins 32 caractères aléatoires (openssl rand -hex 32)."
                )

        if self.ENVIRONMENT == "production":
            problems = []
            if self.DEBUG:
                problems.append("DEBUG doit être false")
            if self.CREATE_TABLES_ON_STARTUP:
                problems.append("CREATE_TABLES_ON_STARTUP doit être false (utiliser Alembic)")
            if "*" in self.get_allowed_origins():
                problems.append(
                    "ALLOWED_ORIGINS ne peut pas contenir '*' "
                    "(incompatible avec allow_credentials=True)"
                )
            if problems:
                raise ValueError("Configuration de production dangereuse : " + " ; ".join(problems))

        if self.ABSENT_WARNING_SECONDS >= self.ABSENT_TIMEOUT_SECONDS:
            raise ValueError(
                "ABSENT_WARNING_SECONDS doit être inférieur à ABSENT_TIMEOUT_SECONDS"
            )
        return self

    model_config = ConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",  # ignore HOST, PORT et toute variable .env non déclarée
    )


# Instance unique partagée dans tout le projet
settings = Settings()

# Configurer le logging dès l'import
settings.configure_logging()
