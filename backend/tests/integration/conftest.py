"""Integration fixtures: a throw-away PostgreSQL database migrated to head.

Set TEST_DATABASE_URL to a server where the role may CREATE DATABASE, e.g.
    TEST_DATABASE_URL=postgresql+psycopg://hotel:hotel@localhost:5432/postgres
A uniquely named database is created per session and dropped afterwards.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    cfg.cmd_opts = type("Opts", (), {"x": [f"db_url={db_url}"]})()
    return cfg


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL not set")

    name = f"hotel_test_{uuid.uuid4().hex[:8]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    try:
        command.upgrade(_alembic_config(url), "head")
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    eng = create_engine(database_url, pool_size=10)
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def alembic_cfg(database_url: str) -> Config:
    return _alembic_config(database_url)
