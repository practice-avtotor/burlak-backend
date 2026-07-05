"""Alembic environment — reads the DB URL from app.core.config."""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Ensure the backend package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

config = context.config

# Override sqlalchemy.url from application settings
try:
    from app.core.config import get_settings

    db_url = get_settings().db_url
    if not db_url.startswith("sqlite://"):
        db_url = f"sqlite:///{db_url}"
    config.set_main_option("sqlalchemy.url", db_url)
except Exception:
    pass  # Fall back to alembic.ini default

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import models so Base.metadata is populated
from app.db.database import Base  # noqa: E402
from app.db import models  # noqa: F401, E402

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without connecting)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (connect to DB)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # Required for SQLite ALTER TABLE
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
