"""Static verification of Alembic migration script content.

These tests parse the migration file(s) to verify that all contract-required
tables, columns, and operations are present — without running any database.
This provides an additional proof-point that the migration is complete and
correct, even in environments where Docker/PostgreSQL are unavailable.
"""

import pathlib
import re

import pytest

from app.models import Base


MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[2] / "alembic" / "versions"

# Sprint 1 contract: all required tables
CONTRACT_TABLES = {
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


@pytest.fixture(scope="module")
def migration_files():
    """Collect all migration .py files (excluding __pycache__)."""
    files = sorted(MIGRATIONS_DIR.glob("*.py"))
    assert len(files) > 0, f"No migration files found in {MIGRATIONS_DIR}"
    return files


@pytest.fixture(scope="module")
def initial_migration_content(migration_files):
    """Read the content of the initial migration script."""
    # The initial migration is the first file (sorted lexicographically)
    return migration_files[0].read_text()


class TestMigrationScriptContent:
    """Statically verify migration script completeness."""

    def test_migration_has_upgrade_and_downgrade(self, initial_migration_content):
        """Migration defines both upgrade() and downgrade() functions."""
        assert "def upgrade()" in initial_migration_content
        assert "def downgrade()" in initial_migration_content

    def test_migration_creates_all_contract_tables(self, initial_migration_content):
        """Every contract-required table has a corresponding op.create_table() call."""
        # Extract all table names from op.create_table('table_name', ...) calls
        created_tables = set(
            re.findall(r"op\.create_table\(\s*['\"](\w+)['\"]", initial_migration_content)
        )

        missing = CONTRACT_TABLES - created_tables
        assert not missing, (
            f"Migration script missing op.create_table() for contract tables: {sorted(missing)}\n"
            f"Tables found in migration: {sorted(created_tables)}"
        )

    def test_migration_drops_all_contract_tables_in_downgrade(self, initial_migration_content):
        """Every contract-required table has a corresponding op.drop_table() in downgrade."""
        # Extract the downgrade function body
        downgrade_match = re.search(
            r"def downgrade\(\).*?(?=\ndef |\Z)", initial_migration_content, re.DOTALL
        )
        assert downgrade_match, "Could not find downgrade() function body"
        downgrade_body = downgrade_match.group()

        dropped_tables = set(
            re.findall(r"op\.drop_table\(\s*['\"](\w+)['\"]", downgrade_body)
        )

        missing = CONTRACT_TABLES - dropped_tables
        assert not missing, (
            f"Migration downgrade() missing op.drop_table() for: {sorted(missing)}\n"
            f"Tables dropped: {sorted(dropped_tables)}"
        )

    def test_migration_table_count_matches_orm(self, migration_files):
        """Number of tables created across all migrations matches ORM model count."""
        # Collect tables created across ALL migration files
        created_tables: set[str] = set()
        for mf in migration_files:
            content = mf.read_text()
            created_tables.update(
                re.findall(r"op\.create_table\(\s*['\"](\w+)['\"]", content)
            )
        orm_tables = set(Base.metadata.tables.keys())

        assert created_tables == orm_tables, (
            f"Migration/ORM table mismatch.\n"
            f"  In migration but not ORM: {created_tables - orm_tables}\n"
            f"  In ORM but not migration: {orm_tables - created_tables}"
        )

    def test_key_columns_present_in_migration_script(self, initial_migration_content):
        """Spot-check that critical columns appear in the migration script.

        This is a string-level check that important columns are referenced
        in the create_table calls, catching cases where the migration was
        generated from incomplete models.
        """
        # Critical columns that must appear somewhere in the migration
        critical_columns = [
            # Users
            ("users", "email"),
            ("users", "full_name"),
            ("users", "role"),
            # Agent tasks
            ("agent_tasks", "agent_type"),
            ("agent_tasks", "status"),
            ("agent_tasks", "input_data"),
            # Audit
            ("audit_logs", "action"),
            ("audit_logs", "phi_accessed"),
            # Payer rules
            ("payer_rules", "conditions"),
            ("payer_rules", "effective_date"),
        ]

        errors = []
        for table, column in critical_columns:
            # Look for the column within the create_table block for this table
            pattern = rf"op\.create_table\(\s*'{table}'.*?(?=op\.create_table|def downgrade)"
            block_match = re.search(pattern, initial_migration_content, re.DOTALL)
            if not block_match:
                errors.append(f"Could not find create_table block for '{table}'")
                continue
            if f"'{column}'" not in block_match.group():
                errors.append(f"Column '{column}' not found in '{table}' create_table block")

        assert not errors, (
            "Critical columns missing from migration script:\n  " + "\n  ".join(errors)
        )

    def test_migration_has_revision_metadata(self, initial_migration_content):
        """Migration has proper Alembic revision metadata."""
        assert "revision:" in initial_migration_content or "revision =" in initial_migration_content
        assert "down_revision" in initial_migration_content


class TestOrmMetadataConsistency:
    """Verify ORM models are complete and consistent with the contract."""

    def test_orm_defines_all_contract_tables(self):
        """All sprint 1 contract tables are defined as SQLAlchemy models."""
        orm_tables = set(Base.metadata.tables.keys())
        missing = CONTRACT_TABLES - orm_tables
        assert not missing, (
            f"ORM missing contract-required tables: {sorted(missing)}"
        )

    def test_orm_table_count(self):
        """ORM defines at least 20 tables (sprint 1 contract minimum)."""
        orm_tables = set(Base.metadata.tables.keys())
        assert len(orm_tables) >= 20, (
            f"Expected >= 20 ORM tables, got {len(orm_tables)}: {sorted(orm_tables)}"
        )

    def test_all_models_have_primary_key(self):
        """Every ORM table has a primary key defined."""
        for table_name, table in Base.metadata.tables.items():
            pk_cols = [c for c in table.columns if c.primary_key]
            assert len(pk_cols) > 0, f"Table '{table_name}' has no primary key"

    def test_timestamp_mixin_fields_on_core_tables(self):
        """Core tables have created_at and updated_at from TimestampMixin."""
        timestamped_tables = [
            "users", "organizations", "patients", "encounters",
            "agent_tasks", "workflow_executions", "payers",
        ]
        for table_name in timestamped_tables:
            table = Base.metadata.tables[table_name]
            col_names = {c.name for c in table.columns}
            assert "created_at" in col_names, f"'{table_name}' missing created_at"
            assert "updated_at" in col_names, f"'{table_name}' missing updated_at"
