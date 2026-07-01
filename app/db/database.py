import os
from collections.abc import AsyncGenerator, Generator

import aiosqlite
from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings

settings = get_settings()

db_url = settings.db_url
if not db_url.startswith("sqlite://"):
    db_url = f"sqlite:///{db_url}"

engine = create_engine(db_url)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """Initialize the database: create directories, set WAL mode, and create tables."""
    db_path = settings.sqlite_db_path
    if db_path:
        db_dir = os.path.dirname(os.path.abspath(db_path))
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    # Import models locally to register them on Base.metadata
    from app.db import models  # noqa: F401

    # Establish WAL mode and create tables
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.commit()
    Base.metadata.create_all(bind=engine)


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
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()
