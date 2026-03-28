"""E2E test for Alembic migration upgrade/downgrade/upgrade cycle.

Verifies that the migration scripts can cleanly upgrade, downgrade, and
re-upgrade without leaving behind stale state. Uses a temporary SQLite
database with the Alembic config, following the same pattern as the
integration migration tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from alembic.config import Config
from alembic import command

from app.models import Base

# Resolve alembic.ini and script_location relative to backend/ directory,
# not the cwd.  This prevents failures when pytest is invoked from the
# project root or any other directory.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = str(_BACKEND_DIR / "alembic.ini")
_ALEMBIC_DIR = str(_BACKEND_DIR / "alembic")

# Tables that must exist after a successful migration to head.
# This is the canonical set of application tables from the data model.
REQUIRED_TABLES = {
    "users",
    "organizations",
    "patients",
    "encounters",
    "agent_tasks",
    "workflow_executions",
    "hitl_reviews",
    "audit_logs",
    "phi_access_log",
    "payers",
    "payer_rules",
    "clearinghouse_configs",
    "eligibility_checks",
    "scheduling_requests",
    "claims",
    "claim_denials",
    "prior_auth_requests",
    "prior_auth_appeals",
    "credentialing_applications",
    "compliance_reports",
}


@pytest.fixture
def alembic_env(tmp_path):
    """Create an Alembic config pointing at a fresh temporary SQLite database."""
    db_path = tmp_path / "e2e_migration_test.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"

    cfg = Config(_ALEMBIC_INI)
    cfg.set_main_option("sqlalchemy.url", async_url)
    cfg.set_main_option("script_location", _ALEMBIC_DIR)
    return cfg, sync_url


def _get_app_tables(sync_url: str) -> set[str]:
    """Return the set of application tables (excluding alembic_version)."""
    engine = create_engine(sync_url)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    engine.dispose()
    tables.discard("alembic_version")
    return tables


def _get_table_columns(sync_url: str) -> dict[str, set[str]]:
    """Return a map of table name -> set of column names."""
    engine = create_engine(sync_url)
    inspector = inspect(engine)
    result = {}
    for table in inspector.get_table_names():
        if table == "alembic_version":
            continue
        result[table] = {col["name"] for col in inspector.get_columns(table)}
    engine.dispose()
    return result


def test_full_migration_cycle(alembic_env):
    """Upgrade -> verify tables -> downgrade -> verify empty -> upgrade again -> verify tables.

    This is the definitive round-trip test that proves migration idempotency:
    the schema after a fresh upgrade is identical to the schema after
    upgrade -> downgrade -> upgrade.
    """
    cfg, sync_url = alembic_env

    # ── Phase 1: Upgrade to head ──────────────────────────────────────
    command.upgrade(cfg, "head")

    tables_after_first_upgrade = _get_app_tables(sync_url)
    columns_after_first_upgrade = _get_table_columns(sync_url)

    # Verify all required tables exist
    missing = REQUIRED_TABLES - tables_after_first_upgrade
    assert not missing, (
        f"Tables missing after initial upgrade: {sorted(missing)}"
    )
    assert len(tables_after_first_upgrade) >= len(REQUIRED_TABLES), (
        f"Expected at least {len(REQUIRED_TABLES)} tables, "
        f"got {len(tables_after_first_upgrade)}"
    )

    # ── Phase 2: Downgrade to base ────────────────────────────────────
    command.downgrade(cfg, "base")

    tables_after_downgrade = _get_app_tables(sync_url)
    assert len(tables_after_downgrade) == 0, (
        f"Tables remain after downgrade to base: {sorted(tables_after_downgrade)}"
    )

    # ── Phase 3: Upgrade to head again ────────────────────────────────
    command.upgrade(cfg, "head")

    tables_after_second_upgrade = _get_app_tables(sync_url)
    columns_after_second_upgrade = _get_table_columns(sync_url)

    # Verify tables match the first upgrade exactly
    assert tables_after_first_upgrade == tables_after_second_upgrade, (
        f"Table sets differ between first and second upgrade.\n"
        f"  Only in first:  {sorted(tables_after_first_upgrade - tables_after_second_upgrade)}\n"
        f"  Only in second: {sorted(tables_after_second_upgrade - tables_after_first_upgrade)}"
    )

    # Verify columns match exactly for every table
    assert columns_after_first_upgrade == columns_after_second_upgrade, (
        "Column sets differ between first and second upgrade"
    )

    # Re-verify all required tables still present
    missing_second = REQUIRED_TABLES - tables_after_second_upgrade
    assert not missing_second, (
        f"Tables missing after second upgrade: {sorted(missing_second)}"
    )


def test_upgrade_creates_key_columns(alembic_env):
    """Verify that critical tables have their expected columns after upgrade.

    This goes beyond table existence to validate the schema shape that
    application code depends on.
    """
    cfg, sync_url = alembic_env

    command.upgrade(cfg, "head")

    expected_columns = {
        "agent_tasks": {"id", "agent_type", "status", "input_data", "output_data", "created_at"},
        "audit_logs": {"id", "action", "actor_type", "resource_type", "phi_accessed", "timestamp"},
        "hitl_reviews": {"id", "task_id", "status", "reviewer_id"},
        "workflow_executions": {"id", "workflow_id", "agent_type", "status"},
        "users": {"id", "email", "full_name", "role", "organization_id"},
        "patients": {"id", "mrn", "first_name", "last_name", "date_of_birth"},
        "payers": {"id", "name", "payer_id_code", "payer_type"},
    }

    engine = create_engine(sync_url)
    inspector = inspect(engine)

    errors = []
    for table_name, required_cols in expected_columns.items():
        actual_cols = {col["name"] for col in inspector.get_columns(table_name)}
        missing_cols = required_cols - actual_cols
        if missing_cols:
            errors.append(
                f"  {table_name}: missing {sorted(missing_cols)} "
                f"(has: {sorted(actual_cols)})"
            )

    engine.dispose()

    assert not errors, (
        "Key columns missing after migration:\n" + "\n".join(errors)
    )


def test_downgrade_removes_all_tables(alembic_env):
    """Verify downgrade to base produces a completely empty database."""
    cfg, sync_url = alembic_env

    command.upgrade(cfg, "head")

    # Sanity check: tables exist before downgrade
    tables_before = _get_app_tables(sync_url)
    assert len(tables_before) > 0, "No tables after upgrade (sanity check failed)"

    command.downgrade(cfg, "base")

    tables_after = _get_app_tables(sync_url)
    assert len(tables_after) == 0, (
        f"Downgrade did not remove all tables. Remaining: {sorted(tables_after)}"
    )


def test_orm_metadata_matches_migration(alembic_env):
    """Verify the set of tables from Alembic migration matches ORM Base.metadata.

    Catches migration drift where models and migration scripts diverge.
    """
    cfg, sync_url = alembic_env

    command.upgrade(cfg, "head")

    migration_tables = _get_app_tables(sync_url)
    orm_tables = set(Base.metadata.tables.keys())

    assert orm_tables == migration_tables, (
        f"Migration drift detected!\n"
        f"  In ORM but not migration: {sorted(orm_tables - migration_tables)}\n"
        f"  In migration but not ORM: {sorted(migration_tables - orm_tables)}"
    )
