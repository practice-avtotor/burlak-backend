import logging
import os
from collections.abc import AsyncGenerator, Generator

import aiosqlite
from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

db_url = settings.db_url
if not db_url.startswith("sqlite://"):
    db_url = f"sqlite:///{db_url}"

engine = create_engine(db_url, connect_args={"timeout": 30, "check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """Initialize the database: create directories, set WAL mode, run Alembic migrations.

    For fresh databases: creates all tables via Alembic ``upgrade head``.
    For existing databases: stamps the current revision and runs any pending
    migrations so the schema is always up-to-date.
    """
    db_path = settings.sqlite_db_path
    if db_path:
        db_dir = os.path.dirname(os.path.abspath(db_path))
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    # Import models locally to register them on Base.metadata
    from app.db import models  # noqa: F401

    # Set WAL mode + busy_timeout
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA busy_timeout=5000"))
        conn.commit()

    # Determine if this is a fresh database (no tables yet)
    is_fresh = False
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'")
            )
            is_fresh = result.fetchone() is None
    except Exception:
        is_fresh = True

    if is_fresh:
        # Fresh DB: create all tables, then stamp as current
        Base.metadata.create_all(bind=engine)
        logger.info("Fresh database — tables created, stamping head revision")
        _stamp_head()
    else:
        # Existing DB: run pending migrations
        logger.info("Existing database — running Alembic migrations")
        _upgrade_head()


def _stamp_head() -> None:
    """Stamp the database with the current Alembic head revision."""
    try:
        from alembic.config import Config
        from alembic import command

        alembic_cfg = _get_alembic_config()
        command.stamp(alembic_cfg, "head")
    except Exception as e:
        logger.warning("Failed to stamp Alembic head: %s", e)


def _upgrade_head() -> None:
    """Run all pending Alembic migrations."""
    try:
        from alembic import command

        alembic_cfg = _get_alembic_config()
        command.upgrade(alembic_cfg, "head")
    except Exception as e:
        logger.warning("Alembic migration failed: %s", e)
        # Fallback: create tables directly (safe for SQLite — IF NOT EXISTS)
        logger.info("Falling back to create_all for schema recovery")
        Base.metadata.create_all(bind=engine)
        _stamp_head()


def _get_alembic_config() -> "Config":
    """Build an Alembic Config pointing at our alembic.ini."""
    from alembic.config import Config

    # alembic.ini lives next to pyproject.toml in the backend root
    backend_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ini_path = os.path.join(backend_root, "alembic.ini")
    alembic_cfg = Config(ini_path)
    alembic_cfg.set_main_option("script_location", os.path.join(backend_root, "alembic"))
    return alembic_cfg


def get_db() -> Generator[Session, None, None]:
    """Synchronous database session (for Celery workers)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def get_async_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Asynchronous database connection (for FastAPI endpoints).

    Provides an aiosqlite connection to the SQLite database.
    The connection is closed when the request is finished.
    """
    db_path = settings.sqlite_db_path
    db = await aiosqlite.connect(db_path, timeout=30.0)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=5000")
    try:
        yield db
    finally:
        await db.close()
