"""Alembic environment.

Runs migrations synchronously with psycopg 3. A transaction-level advisory lock
serialises concurrent `alembic upgrade` invocations (e.g. several replicas
booting at once) so the schema is never migrated twice in parallel.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool, text

from app.core.config import get_settings
from app.infrastructure.db import models  # noqa: F401  (registers tables on metadata)
from app.infrastructure.db.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Arbitrary, project-unique 64-bit key for pg_advisory_xact_lock.
_MIGRATION_LOCK_KEY = 7_041_202_601


def _database_url() -> str:
    # Allow `alembic -x db_url=...` (used by integration tests), else env settings.
    override = context.get_x_argument(as_dictionary=True).get("db_url")
    return override or str(get_settings().database_url)


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            transaction_per_migration=False,
        )
        with context.begin_transaction():
            # CONCURRENCY: only one migrator at a time; others block here and
            # then find the schema already at head.
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": _MIGRATION_LOCK_KEY}
            )
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
