"""
Connexion à la base de données PostgreSQL.
On utilise SQLAlchemy en mode asynchrone pour de meilleures performances.
"""
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.core.config import settings


# ── Moteur de base de données ─────────────────────────────────────────────────
# SQLite (tests locaux, outillage) ne supporte pas pool_size/max_overflow.
# En production PostgreSQL, on garde un pool dimensionné pour un seul worker.
_engine_options: dict = {
    "echo": settings.DEBUG,   # Affiche les requêtes SQL en mode debug
}

# Neon / Supabase / Render managé imposent TLS. On lit l'exigence AVANT de
# nettoyer l'URL, puis on la transmet à asyncpg via connect_args — asyncpg ne
# sait pas lire « sslmode » et lèverait TypeError au premier SELECT.
_database_url = settings.adapt_url_for_asyncpg(settings.DATABASE_URL)

if not _database_url.startswith("sqlite"):
    _engine_options.update(
        pool_size=10,         # Nombre de connexions simultanées
        max_overflow=20,      # Connexions supplémentaires si besoin
        pool_pre_ping=True,   # Remplace une connexion morte après une coupure
    )
    if settings.url_requires_ssl(settings.DATABASE_URL):
        # ssl=True : vérification complète du certificat serveur (CAs système).
        # Neon présente un certificat public, la vérification passe.
        _engine_options["connect_args"] = {"ssl": True}

engine = create_async_engine(_database_url, **_engine_options)

# ── Fabrique de sessions ──────────────────────────────────────────────────────
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# ── Classe de base pour tous les modèles ─────────────────────────────────────
class Base(DeclarativeBase):
    pass


# ── Dépendance FastAPI — injectée dans chaque route ──────────────────────────
async def get_db() -> AsyncSession:
    """
    Fournit une session DB à chaque requête API.
    La session est fermée automatiquement après la requête.

    Utilisation dans un router :
        async def mon_endpoint(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ── Créer toutes les tables ───────────────────────────────────────────────────
async def create_tables():
    """Appelé au démarrage de l'app pour créer les tables si elles n'existent pas."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
