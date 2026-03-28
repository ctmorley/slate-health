"""Add database-level immutability triggers for audit tables.

Revision ID: 3bcd135f3633
Revises: 2acf024f2522
Create Date: 2026-03-26 20:00:00.000000

Enforces immutability of audit_logs and phi_access_log at the database level
via triggers that reject UPDATE and DELETE operations. This is a defense-in-depth
measure complementing the ORM-level event listeners.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3bcd135f3633'
down_revision: Union[str, None] = '2acf024f2522'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        # Create a function that raises an exception to block modifications
        op.execute(sa.text("""
            CREATE OR REPLACE FUNCTION prevent_audit_modification()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION 'Audit records are immutable: % operations are not allowed on %',
                    TG_OP, TG_TABLE_NAME;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
        """))

        # Attach triggers to audit_logs
        op.execute(sa.text("""
            CREATE TRIGGER audit_logs_no_update
            BEFORE UPDATE ON audit_logs
            FOR EACH ROW EXECUTE FUNCTION prevent_audit_modification();
        """))
        op.execute(sa.text("""
            CREATE TRIGGER audit_logs_no_delete
            BEFORE DELETE ON audit_logs
            FOR EACH ROW EXECUTE FUNCTION prevent_audit_modification();
        """))

        # Attach triggers to phi_access_log
        op.execute(sa.text("""
            CREATE TRIGGER phi_access_log_no_update
            BEFORE UPDATE ON phi_access_log
            FOR EACH ROW EXECUTE FUNCTION prevent_audit_modification();
        """))
        op.execute(sa.text("""
            CREATE TRIGGER phi_access_log_no_delete
            BEFORE DELETE ON phi_access_log
            FOR EACH ROW EXECUTE FUNCTION prevent_audit_modification();
        """))

    elif dialect == "sqlite":
        # SQLite triggers for test environments
        for table in ("audit_logs", "phi_access_log"):
            op.execute(sa.text(f"""
                CREATE TRIGGER IF NOT EXISTS {table}_no_update
                BEFORE UPDATE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, 'Audit records are immutable: UPDATE operations are not allowed on {table}');
                END;
            """))
            op.execute(sa.text(f"""
                CREATE TRIGGER IF NOT EXISTS {table}_no_delete
                BEFORE DELETE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, 'Audit records are immutable: DELETE operations are not allowed on {table}');
                END;
            """))


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        op.execute(sa.text("DROP TRIGGER IF EXISTS audit_logs_no_update ON audit_logs;"))
        op.execute(sa.text("DROP TRIGGER IF EXISTS audit_logs_no_delete ON audit_logs;"))
        op.execute(sa.text("DROP TRIGGER IF EXISTS phi_access_log_no_update ON phi_access_log;"))
        op.execute(sa.text("DROP TRIGGER IF EXISTS phi_access_log_no_delete ON phi_access_log;"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS prevent_audit_modification();"))

    elif dialect == "sqlite":
        for table in ("audit_logs", "phi_access_log"):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {table}_no_update;"))
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {table}_no_delete;"))
