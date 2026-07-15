"""Alembic migration environment.

Database configuration and connections are created only when Alembic executes this
file as its migration environment, never when it is imported as a normal module.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from dashboard.app.db import create_database_engine, get_database_settings
from src.crm.persistence.base import Base
import src.crm.persistence.models  # noqa: F401  # register tables for autogenerate


target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without creating an Engine."""

    settings = get_database_settings()
    context.configure(
        url=settings.database_url.get_secret_value(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations with an explicitly created Engine and connection."""

    engine = create_database_engine(get_database_settings())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


def run_migrations() -> None:
    """Configure logging and dispatch the requested Alembic mode."""

    config = context.config
    if config.config_file_name is not None:
        fileConfig(config.config_file_name, disable_existing_loggers=False)

    if context.is_offline_mode():
        run_migrations_offline()
    else:
        run_migrations_online()


# Alembic loads env.py with this module name. Normal imports remain side-effect free.
if __name__ == "env_py":
    run_migrations()
