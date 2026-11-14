"""
Normalisation des URLs de base de données et séparation API / migrations.

Ces règles sont la seule chose qui empêche un démarrage en erreur sur une base
managée : Neon, Supabase et Render injectent des URLs que SQLAlchemy async
n'accepte pas telles quelles (``postgres://``, ``sslmode``, ``channel_binding``).
"""
import pytest

from app.core.config import Settings

JWT = "0123456789012345678901234567890123456789"

NEON_POOLED = (
    "postgresql://user:AbC%40123@ep-cool-darkness-123456-pooler.eu-central-1.aws.neon.tech/neondb"
    "?sslmode=require&channel_binding=require"
)
NEON_DIRECT = (
    "postgresql://user:AbC%40123@ep-cool-darkness-123456.eu-central-1.aws.neon.tech/neondb"
    "?sslmode=require"
)


def _settings(**kwargs) -> Settings:
    kwargs.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
    return Settings(JWT_SECRET_KEY=JWT, **kwargs)


# ── Normalisation du driver ─────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expected_prefix",
    [
        ("postgres://u:p@h:5432/db", "postgresql+asyncpg://"),
        ("postgresql://u:p@h:5432/db", "postgresql+asyncpg://"),
        ("postgresql+asyncpg://u:p@h:5432/db", "postgresql+asyncpg://"),
        ("sqlite+aiosqlite:///./t.db", "sqlite+aiosqlite:///"),
    ],
)
def test_driver_is_normalized(raw, expected_prefix):
    """``postgres://`` et ``postgresql://`` deviennent ``postgresql+asyncpg://``."""
    assert _settings(DATABASE_URL=raw).DATABASE_URL.startswith(expected_prefix)


def test_migration_url_is_normalized_too():
    """La seconde URL suit exactement les mêmes règles."""
    settings = _settings(MIGRATION_DATABASE_URL=NEON_DIRECT)
    assert settings.MIGRATION_DATABASE_URL.startswith("postgresql+asyncpg://")


# ── Paramètres libpq retirés avant asyncpg ─────────────────────────────────

def test_libpq_only_params_are_stripped():
    """
    asyncpg refuse ``sslmode`` et ``channel_binding`` (« unexpected keyword
    argument »). Ils doivent disparaître de l'URL transmise au moteur.
    """
    settings = _settings(DATABASE_URL=NEON_POOLED)
    adapted = settings.adapt_url_for_asyncpg(settings.DATABASE_URL)

    assert "sslmode" not in adapted
    assert "channel_binding" not in adapted
    # Le reste de l'URL ne doit pas bouger.
    assert "ep-cool-darkness-123456-pooler.eu-central-1.aws.neon.tech" in adapted
    assert adapted.endswith("/neondb")


def test_unrelated_query_params_are_preserved():
    """Seuls les paramètres libpq sont retirés, pas les autres."""
    settings = _settings()
    url = "postgresql://u:p@h:5432/db?sslmode=require&application_name=tonde"
    adapted = settings.adapt_url_for_asyncpg(url)

    assert "sslmode" not in adapted
    assert "application_name=tonde" in adapted


def test_encoded_password_survives_adaptation():
    """Un mot de passe encodé (``%40``) ne doit pas être décodé ni perdu."""
    settings = _settings(DATABASE_URL=NEON_POOLED)
    adapted = settings.adapt_url_for_asyncpg(settings.DATABASE_URL)

    assert "AbC%40123" in adapted


# ── Détection TLS ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "url,expected",
    [
        (NEON_POOLED, True),
        (NEON_DIRECT, True),
        ("postgresql://u:p@ep-x.eu-central-1.aws.neon.tech/neondb", True),
        ("postgresql://u:p@db.xxxx.supabase.co:5432/postgres", True),
        ("postgresql://u:p@localhost:5432/tonde", False),
        ("sqlite+aiosqlite:///./t.db", False),
    ],
)
def test_ssl_detection(url, expected):
    """TLS est exigé par Neon et Supabase, pas par une base locale."""
    assert _settings().url_requires_ssl(url) is expected


# ── Séparation API / migrations ────────────────────────────────────────────

def test_migration_url_falls_back_to_database_url():
    """Sans MIGRATION_DATABASE_URL, Alembic utilise DATABASE_URL."""
    settings = _settings(DATABASE_URL=NEON_POOLED)
    assert settings.MIGRATION_DATABASE_URL == ""
    assert settings.MIGRATION_URL == settings.DATABASE_URL


def test_migration_url_can_point_elsewhere():
    """
    Cas Neon : API sur le pooler, migrations sur l'endpoint direct.

    Le pooler en mode transaction ne conserve pas les paramètres de session dont
    un ``alembic upgrade`` a besoin ; Neon recommande la connexion directe pour
    les migrations et les dumps.
    """
    settings = _settings(DATABASE_URL=NEON_POOLED, MIGRATION_DATABASE_URL=NEON_DIRECT)

    assert "-pooler" in settings.DATABASE_URL
    assert "-pooler" not in settings.MIGRATION_URL
    assert settings.MIGRATION_URL.startswith("postgresql+asyncpg://")
