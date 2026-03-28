"""Integration test: verify Alembic migrations match ORM metadata.

This catches migration drift — where the SQLAlchemy models and the Alembic
migration scripts define different schemas.

Uses aiosqlite to be compatible with the async Alembic env.py.  These tests
run **without Docker** and serve as a primary proof-point that Alembic
migrations produce the correct schema for all sprint-1 tables.
"""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from alembic.config import Config
from alembic import command

from app.models import Base

# Resolve alembic.ini relative to the backend/ directory, not the cwd.
# This prevents FileNotFoundError when pytest is invoked from a parent directory.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = str(_BACKEND_DIR / "alembic.ini")


# The exact set of tables required by the sprint 1 contract.
EXPECTED_CONTRACT_TABLES = {
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

# Key columns that must exist on critical tables (contract-specified fields).
EXPECTED_KEY_COLUMNS = {
    "users": {"id", "email", "full_name", "role", "organization_id", "created_at", "updated_at"},
    "organizations": {"id", "name", "npi", "tax_id", "created_at", "updated_at"},
    "patients": {"id", "mrn", "first_name", "last_name", "date_of_birth", "organization_id"},
    "encounters": {"id", "patient_id", "encounter_type", "encounter_date", "status"},
    "agent_tasks": {"id", "agent_type", "status", "input_data", "output_data", "created_at"},
    "workflow_executions": {"id", "workflow_id", "agent_type", "status"},
    "hitl_reviews": {"id", "task_id", "status", "reviewer_id"},
    "audit_logs": {"id", "action", "actor_type", "resource_type", "phi_accessed", "timestamp"},
    "phi_access_log": {"id", "user_id", "resource_type", "reason"},
    "payers": {"id", "name", "payer_id_code", "payer_type"},
    "payer_rules": {"id", "payer_id", "agent_type", "rule_type", "conditions", "effective_date"},
    "clearinghouse_configs": {"id", "organization_id", "clearinghouse_name"},
    "eligibility_checks": {"id", "patient_id", "payer_id", "status"},
    "claims": {"id", "encounter_id", "payer_id", "status", "claim_type"},
    "prior_auth_requests": {"id", "patient_id", "payer_id", "status"},
}


@pytest.fixture
def alembic_config(tmp_path):
    """Create an Alembic config pointing at a temporary SQLite database."""
    db_path = tmp_path / "test_migration.db"
    # Use aiosqlite driver to match the async engine in alembic/env.py
    db_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"

    cfg = Config(_ALEMBIC_INI)
    cfg.set_main_option("sqlalchemy.url", db_url)
    # Resolve script_location to absolute path so tests work regardless of cwd.
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    return cfg, sync_url, db_path


def test_alembic_upgrade_creates_all_expected_tables(alembic_config):
    """Running 'alembic upgrade head' produces the same tables as ORM metadata."""
    cfg, sync_url, _ = alembic_config

    # Run migrations via Alembic (uses async engine internally)
    command.upgrade(cfg, "head")

    # Inspect the resulting schema using sync engine (same file, different driver)
    engine = create_engine(sync_url)
    inspector = inspect(engine)
    migration_tables = set(inspector.get_table_names())
    engine.dispose()

    # Get the expected tables from ORM metadata
    orm_tables = set(Base.metadata.tables.keys())

    # Alembic adds its own version table
    migration_tables.discard("alembic_version")

    assert orm_tables == migration_tables, (
        f"Migration drift detected!\n"
        f"  Tables in ORM but not in migration: {orm_tables - migration_tables}\n"
        f"  Tables in migration but not in ORM: {migration_tables - orm_tables}"
    )


def test_alembic_creates_all_contract_required_tables(alembic_config):
    """Every table listed in the sprint 1 contract exists after migration.

    This explicitly checks the 20 tables specified in the sprint contract,
    providing a Docker-free proof that the migration is complete.
    """
    cfg, sync_url, _ = alembic_config

    command.upgrade(cfg, "head")

    engine = create_engine(sync_url)
    inspector = inspect(engine)
    migration_tables = set(inspector.get_table_names())
    engine.dispose()

    missing = EXPECTED_CONTRACT_TABLES - migration_tables
    assert not missing, (
        f"Sprint contract tables missing after Alembic migration: {sorted(missing)}"
    )
    # Also verify we have at least 20 tables (the contract minimum)
    actual_app_tables = migration_tables - {"alembic_version"}
    assert len(actual_app_tables) >= 20, (
        f"Expected at least 20 tables, got {len(actual_app_tables)}: {sorted(actual_app_tables)}"
    )


def test_alembic_creates_key_columns_on_critical_tables(alembic_config):
    """Verify that key columns exist on critical tables after migration.

    This provides schema-level proof without requiring PostgreSQL: the Alembic
    migration creates not just the tables but the correct columns that the
    application code depends on.
    """
    cfg, sync_url, _ = alembic_config

    command.upgrade(cfg, "head")

    engine = create_engine(sync_url)
    inspector = inspect(engine)

    errors = []
    for table_name, expected_cols in EXPECTED_KEY_COLUMNS.items():
        actual_cols = {col["name"] for col in inspector.get_columns(table_name)}
        missing_cols = expected_cols - actual_cols
        if missing_cols:
            errors.append(f"  {table_name}: missing columns {sorted(missing_cols)}")

    engine.dispose()

    assert not errors, (
        "Key columns missing after Alembic migration:\n" + "\n".join(errors)
    )


def test_alembic_creates_foreign_keys_on_key_tables(alembic_config):
    """Verify critical foreign key relationships exist after migration."""
    cfg, sync_url, _ = alembic_config

    command.upgrade(cfg, "head")

    engine = create_engine(sync_url)
    inspector = inspect(engine)

    # Map of table -> expected referred tables (at least these FK relationships)
    expected_fks = {
        "users": {"organizations"},
        "patients": {"organizations"},
        "encounters": {"patients"},
        "agent_tasks": {"organizations"},
        "hitl_reviews": {"agent_tasks"},
        "payer_rules": {"payers"},
        "eligibility_checks": {"patients", "payers"},
        "claims": {"payers"},
    }

    errors = []
    for table_name, expected_referred in expected_fks.items():
        fks = inspector.get_foreign_keys(table_name)
        actual_referred = {fk["referred_table"] for fk in fks}
        missing = expected_referred - actual_referred
        if missing:
            errors.append(
                f"  {table_name}: missing FK to {sorted(missing)} "
                f"(has FKs to {sorted(actual_referred)})"
            )

    engine.dispose()

    assert not errors, (
        "Expected foreign keys missing after Alembic migration:\n" + "\n".join(errors)
    )


def test_alembic_upgrade_downgrade_cycle(alembic_config):
    """Alembic upgrade then downgrade completes without errors."""
    cfg, sync_url, _ = alembic_config

    # Upgrade to head
    command.upgrade(cfg, "head")

    engine = create_engine(sync_url)
    inspector = inspect(engine)
    tables_after_upgrade = set(inspector.get_table_names())
    tables_after_upgrade.discard("alembic_version")
    assert len(tables_after_upgrade) > 0, "No tables created after upgrade"
    engine.dispose()

    # Downgrade back to base
    command.downgrade(cfg, "base")

    engine = create_engine(sync_url)
    inspector = inspect(engine)
    tables_after_downgrade = set(inspector.get_table_names())
    tables_after_downgrade.discard("alembic_version")
    assert len(tables_after_downgrade) == 0, (
        f"Tables remain after downgrade: {tables_after_downgrade}"
    )
    engine.dispose()


def test_alembic_full_round_trip_produces_identical_schema(alembic_config):
    """Upgrade → downgrade → upgrade produces identical schema.

    This is the same critical test as in the PostgreSQL test suite,
    adapted for SQLite. It proves idempotency of the migration.
    """
    cfg, sync_url, _ = alembic_config

    # First upgrade
    command.upgrade(cfg, "head")

    engine = create_engine(sync_url)
    inspector = inspect(engine)
    tables_first = set(inspector.get_table_names())
    tables_first.discard("alembic_version")
    columns_first = {
        table: {col["name"] for col in inspector.get_columns(table)}
        for table in sorted(tables_first)
    }
    engine.dispose()

    # Downgrade
    command.downgrade(cfg, "base")

    # Second upgrade
    command.upgrade(cfg, "head")

    engine = create_engine(sync_url)
    inspector = inspect(engine)
    tables_second = set(inspector.get_table_names())
    tables_second.discard("alembic_version")
    columns_second = {
        table: {col["name"] for col in inspector.get_columns(table)}
        for table in sorted(tables_second)
    }
    engine.dispose()

    assert tables_first == tables_second, (
        f"Round-trip produced different table sets.\n"
        f"  First: {sorted(tables_first)}\n"
        f"  Second: {sorted(tables_second)}"
    )
    assert columns_first == columns_second, (
        "Round-trip produced different column sets on one or more tables."
    )
