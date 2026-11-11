"""Alembic environment: migrates the database named by WAE_DATABASE_URL."""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from app import models  # noqa: F401 - registers every table on Base.metadata
from app.db import Base, get_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_online() -> None:
    engine = get_engine()
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
            user_module_prefix="app.db.",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("Offline migrations are not supported; set WAE_DATABASE_URL and run online.")
run_migrations_online()
