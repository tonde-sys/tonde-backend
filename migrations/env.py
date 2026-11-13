"""
Alembic env.py — Configuration des migrations pour TONDE.

Ce fichier est exécuté par Alembic lors de la génération
et de l'application des migrations.

Configuré pour :
  - SQLAlchemy async (asyncpg)
  - Autogenerate depuis les modèles SQLAlchemy
  - Import explicite de tous les modèles pour que
    Base.metadata les connaisse
"""
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# ── Import de la config TONDE ─────────────────────────────────────────────────
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings
from app.core.database import Base

# ── Import EXPLICITE de tous les modèles ─────────────────────────────────────
# Obligatoire : Alembic ne détecte que les modèles importés dans Base.metadata
# Ajouter ici chaque nouveau modèle créé
from app.models import (  # noqa: F401
    organization,
    user,
    branch,
    counter,
    employee,
    ticket,
    refresh_token,
    user_organization,
    queue_log,
    notification,
    audit_log,
)

# ── Configuration Alembic ─────────────────────────────────────────────────────
config = context.config

# Injecter l'URL DB depuis les settings TONDE (lit le .env)
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

# Logger depuis alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata cible pour l'autogenerate
target_metadata = Base.metadata


# ── Mode offline (génération SQL sans connexion DB) ───────────────────────────
def run_migrations_offline() -> None:
    """
    Génère les scripts SQL sans se connecter à la base.
    Utile pour auditer les migrations avant de les appliquer.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ── Mode online async (application réelle des migrations) ────────────────────
def _ensure_wide_version_column(connection: Connection) -> None:
    """
    Élargit alembic_version.version_num à VARCHAR(255).

    Alembic le crée en VARCHAR(32), or nos identifiants de révision sont plus
    longs (ex. '006_add_notifications_and_queue_logs', 36 caractères) : sans
    cela, l'application des migrations échoue depuis une base vide.
    Idempotent : sans effet si la colonne est déjà assez large.
    """
    connection.execute(text(
        "CREATE TABLE IF NOT EXISTS alembic_version ("
        "version_num VARCHAR(255) NOT NULL, "
        "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
    ))
    connection.execute(text(
        "ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(255)"
    ))
    connection.commit()


def do_run_migrations(connection: Connection) -> None:
    _ensure_wide_version_column(connection)
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Lance les migrations avec le moteur async asyncpg."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


# ── Point d'entrée ────────────────────────────────────────────────────────────
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
