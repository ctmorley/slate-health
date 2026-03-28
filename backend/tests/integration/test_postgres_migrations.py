"""Integration tests that run Alembic migrations against a real PostgreSQL instance.

Uses testcontainers to spin up a disposable PostgreSQL 16 container, then
verifies upgrade, table inspection, and full downgrade/upgrade round-trip
— including enum type cleanup.

Skip behaviour:
- In CI with Docker available (``DOCKER_AVAILABLE=1``): these tests are a
  **release gate** — skipping them fails the suite.
- In CI without Docker: tests skip with a warning.  Docker-free integration
  tests (e.g. ``test_alembic_migrations.py``) still run and pass.
- Locally (no ``CI`` env var): tests are skipped gracefully so developers
  without Docker can still run the rest of the suite.
"""

from pathlib import Path

import pytest

from tests.integration.conftest import POSTGRES_DEPS_AVAILABLE  # shared check

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not POSTGRES_DEPS_AVAILABLE,
        reason="testcontainers and/or Docker not available (local dev — skipping)",
    ),
]

from sqlalchemy import create_engine, inspect, text
from alembic.config import Config
from alembic import command

from app.models import Base

# Resolve alembic.ini and script_location relative to backend/ directory,
# not the cwd.  This prevents failures when pytest is invoked from the
# project root or any other directory.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = str(_BACKEND_DIR / "alembic.ini")
_ALEMBIC_DIR = str(_BACKEND_DIR / "alembic")

if POSTGRES_DEPS_AVAILABLE:
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

# The enum types created by the initial migration.
EXPECTED_ENUM_TYPES = {
    "agent_type_enum",
    "task_status_enum",
    "workflow_status_enum",
    "user_role",
    "review_status_enum",
}


@pytest.fixture(scope="module")
def postgres_container():
    """Start a PostgreSQL 16 container for the module's test session."""
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest.fixture()
def pg_alembic_config(postgres_container):
    """Create an Alembic config pointing at the testcontainers PostgreSQL.

    Returns (alembic_cfg, sync_url).
    """
    # testcontainers gives us a psycopg2 sync URL like:
    #   postgresql+psycopg2://test:test@localhost:32791/test
    sync_url = postgres_container.get_connection_url()
    # Alembic env.py expects an *async* URL (asyncpg driver)
    async_url = sync_url.replace("+psycopg2", "+asyncpg", 1)

    cfg = Config(_ALEMBIC_INI)
    cfg.set_main_option("sqlalchemy.url", async_url)
    cfg.set_main_option("script_location", _ALEMBIC_DIR)
    return cfg, sync_url


def _get_pg_enum_names(sync_url: str) -> set[str]:
    """Query pg_type for user-defined enum type names."""
    engine = create_engine(sync_url)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT typname FROM pg_type "
                "WHERE typcategory = 'E' "
                "AND typname NOT LIKE 'pg_%'"
            )
        ).fetchall()
    engine.dispose()
    return {r[0] for r in rows}


# ── Tests ────────────────────────────────────────────────────────────


def test_upgrade_creates_all_tables_on_postgres(pg_alembic_config):
    """Alembic upgrade head creates every ORM table on real PostgreSQL."""
    cfg, sync_url = pg_alembic_config

    command.upgrade(cfg, "head")

    engine = create_engine(sync_url)
    inspector = inspect(engine)
    migration_tables = set(inspector.get_table_names())
    engine.dispose()

    orm_tables = set(Base.metadata.tables.keys())
    migration_tables.discard("alembic_version")

    assert orm_tables == migration_tables, (
        f"Migration drift on PostgreSQL!\n"
        f"  In ORM but not migrated: {orm_tables - migration_tables}\n"
        f"  Migrated but not in ORM: {migration_tables - orm_tables}"
    )


def test_upgrade_creates_enum_types_on_postgres(pg_alembic_config):
    """Verify that PostgreSQL enum types are created during upgrade."""
    cfg, sync_url = pg_alembic_config

    # upgrade may already have run in a prior test; ensure we're at head
    command.upgrade(cfg, "head")

    enum_names = _get_pg_enum_names(sync_url)
    assert EXPECTED_ENUM_TYPES.issubset(enum_names), (
        f"Missing enum types on PostgreSQL: {EXPECTED_ENUM_TYPES - enum_names}"
    )


def test_downgrade_removes_tables_and_enums_on_postgres(pg_alembic_config):
    """Full downgrade removes all tables AND enum types on PostgreSQL."""
    cfg, sync_url = pg_alembic_config

    # Make sure we're at head first
    command.upgrade(cfg, "head")

    # Downgrade to base
    command.downgrade(cfg, "base")

    engine = create_engine(sync_url)
    inspector = inspect(engine)
    remaining_tables = set(inspector.get_table_names())
    remaining_tables.discard("alembic_version")
    engine.dispose()

    assert len(remaining_tables) == 0, (
        f"Tables remain after downgrade on PostgreSQL: {remaining_tables}"
    )

    # Enum types must also be gone
    remaining_enums = _get_pg_enum_names(sync_url)
    leftover = EXPECTED_ENUM_TYPES & remaining_enums
    assert len(leftover) == 0, (
        f"Enum types remain after downgrade on PostgreSQL: {leftover}"
    )


def test_full_upgrade_downgrade_upgrade_cycle_on_postgres(pg_alembic_config):
    """Upgrade → downgrade → upgrade round-trip succeeds on PostgreSQL.

    This is the critical test that would fail if downgrade didn't clean up
    enum types — the second upgrade would hit 'type already exists' errors.
    """
    cfg, sync_url = pg_alembic_config

    # Start from a clean base
    command.downgrade(cfg, "base")

    # First upgrade
    command.upgrade(cfg, "head")

    engine = create_engine(sync_url)
    inspector = inspect(engine)
    tables_first = set(inspector.get_table_names())
    tables_first.discard("alembic_version")
    engine.dispose()
    assert len(tables_first) > 0

    # Downgrade
    command.downgrade(cfg, "base")

    # Second upgrade — this proves enums were properly cleaned up
    command.upgrade(cfg, "head")

    engine = create_engine(sync_url)
    inspector = inspect(engine)
    tables_second = set(inspector.get_table_names())
    tables_second.discard("alembic_version")
    engine.dispose()

    assert tables_first == tables_second, "Round-trip produced different table sets"
