"""Pytest configuration with async test client and test database fixtures.

Includes a session-finish hook that warns when PostgreSQL integration tests
are skipped in CI.  The hook only triggers a **hard failure** when Docker is
explicitly available (``DOCKER_AVAILABLE=1``), preventing Docker-free CI runs
from failing due to expected postgres test skips.

Uses pytest-asyncio's recommended ``event_loop_policy`` fixture (instead
of the deprecated ``event_loop`` override) to ensure a single, stable
event loop is shared across the entire session.
"""

import os
import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import date, datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings
from app.dependencies import async_reset_db_state, get_db, get_session_factory
from app.main import create_app
from app.models import Base
import app.dependencies as _deps

# Source test DB URL from Settings so it stays in sync with config.py.
# This respects the SLATE_TEST_DATABASE_URL env var override as well.
_test_settings = Settings(_env_file=None)
TEST_DATABASE_URL = _test_settings.test_database_url

_IN_CI = os.environ.get("CI", "").lower() in ("true", "1", "yes")


# ── Release-gate hook: fail CI if PostgreSQL tests were skipped ─────


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """After the full suite finishes, check whether release-gate tests were
    skipped.  In CI this is a hard failure only when Docker is available but
    tests still skipped (indicating a real bug).  When Docker is unavailable,
    skipped postgres tests are expected and reported as a warning — not a
    failure — so Docker-free CI environments remain green.

    Checks:
    1. PostgreSQL integration tests (require Docker + testcontainers)
       — only a hard failure if Docker IS available (DOCKER_AVAILABLE=1)
    2. Docker E2E tests (require DOCKER_E2E=1 and running Docker Compose stack)
    """
    if not _IN_CI:
        return

    skipped = terminalreporter.stats.get("skipped", [])

    # Check 1: PostgreSQL integration tests
    pg_skipped = [
        rep
        for rep in skipped
        if "test_postgres_migrations" in str(getattr(rep, "nodeid", ""))
    ]
    if pg_skipped:
        # Only treat as a hard failure if Docker is actually available.
        # When Docker is missing, postgres test skips are expected.
        _docker_available = os.environ.get("DOCKER_AVAILABLE", "").lower() in (
            "true", "1", "yes",
        )
        if _docker_available:
            terminalreporter.section("RELEASE GATE FAILURE")
            terminalreporter.write_line(
                f"ERROR: {len(pg_skipped)} PostgreSQL integration test(s) were "
                "skipped in CI despite Docker being available. These tests are "
                "a release gate and must not be skipped.",
            )
            terminalreporter._session.exitstatus = pytest.ExitCode.TESTS_FAILED
        else:
            terminalreporter.section("RELEASE GATE WARNING")
            terminalreporter.write_line(
                f"WARNING: {len(pg_skipped)} PostgreSQL integration test(s) were "
                "skipped because Docker is not available. These tests are a "
                "release gate — run them in a Docker-enabled CI environment "
                "before release (set DOCKER_AVAILABLE=1 to enforce).",
            )

    # Check 2: Docker E2E tests (only when DOCKER_E2E=1 is expected)
    _docker_e2e = os.environ.get("DOCKER_E2E") == "1"
    if _docker_e2e:
        e2e_skipped = [
            rep
            for rep in skipped
            if "test_docker_e2e" in str(getattr(rep, "nodeid", ""))
        ]
        if e2e_skipped:
            terminalreporter.section("RELEASE GATE FAILURE")
            terminalreporter.write_line(
                f"ERROR: {len(e2e_skipped)} Docker E2E test(s) were "
                "skipped in CI despite DOCKER_E2E=1. Ensure Docker "
                "Compose stack is running.",
            )
            terminalreporter._session.exitstatus = pytest.ExitCode.TESTS_FAILED


# ── Event loop configuration ────────────────────────────────────────
# pytest-asyncio is configured in pyproject.toml with:
#   asyncio_default_fixture_loop_scope = "session"
# This ensures a single event loop for the entire session, shared by
# all async tests and fixtures, preventing "There is no current event
# loop" errors across test directories.
#
# The hook below guards against Python 3.12's strict "no current event loop"
# behaviour: if the loop is missing or closed when a new test starts
# (e.g. when switching between test directories), we re-create and
# re-install it so that pytest-asyncio's wrap_in_sync always finds one.


def pytest_runtest_setup(item):  # noqa: D103 – pytest hook
    """Ensure an asyncio event loop is set in the main thread before every test.

    Python 3.12 raises ``RuntimeError`` from ``asyncio.get_event_loop()``
    when no loop is present.  pytest-asyncio's ``wrap_in_sync`` calls that
    function *synchronously* before handing off to the loop, so we must
    guarantee a loop exists.  Re-using an already-open loop is fine;
    creating a fresh one is the fallback.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)


@pytest.fixture(scope="session")
async def test_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Create a test database engine with immutability triggers on audit tables."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Apply DB-level immutability triggers for audit tables (mirrors migration)
        dialect = conn.dialect.name
        if dialect == "sqlite":
            from sqlalchemy import text
            for table in ("audit_logs", "phi_access_log"):
                await conn.execute(text(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_update "
                    f"BEFORE UPDATE ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, 'Audit records are immutable: "
                    f"UPDATE operations are not allowed on {table}'); END;"
                ))
                await conn.execute(text(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete "
                    f"BEFORE DELETE ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, 'Audit records are immutable: "
                    f"DELETE operations are not allowed on {table}'); END;"
                ))
        elif dialect == "postgresql":
            from sqlalchemy import text
            await conn.execute(text("""
                CREATE OR REPLACE FUNCTION prevent_audit_modification()
                RETURNS TRIGGER AS $$
                BEGIN
                    RAISE EXCEPTION 'Audit records are immutable: % operations are not allowed on %',
                        TG_OP, TG_TABLE_NAME;
                    RETURN NULL;
                END;
                $$ LANGUAGE plpgsql;
            """))
            for table in ("audit_logs", "phi_access_log"):
                await conn.execute(text(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_update "
                    f"BEFORE UPDATE ON {table} "
                    f"FOR EACH ROW EXECUTE FUNCTION prevent_audit_modification();"
                ))
                await conn.execute(text(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete "
                    f"BEFORE DELETE ON {table} "
                    f"FOR EACH ROW EXECUTE FUNCTION prevent_audit_modification();"
                ))
    # Register the test engine with the DI module so that activities
    # (which call _get_activity_session_factory) use the test DB rather
    # than attempting to connect to the production PostgreSQL instance.
    _deps._engine = engine
    _deps._session_factory = None  # force rebuild on next access
    get_session_factory(engine)

    yield engine

    # Tear down
    _deps._engine = None
    _deps._session_factory = None
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def db_session(test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional test database session that rolls back after each test."""
    session_factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        yield session
        await session.rollback()


@pytest.fixture
async def client(test_engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
    """Provide an async test client with dependency overrides."""
    session_factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db

    # Patch get_session_factory so the /ready endpoint uses the test DB
    import app.main as main_module

    original_factory = main_module.get_session_factory
    main_module.get_session_factory = lambda: session_factory

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    main_module.get_session_factory = original_factory


# ── Enable mock fallback for tests ────────────────────────────────────
# Production default is allow_mock_fallback=False to prevent silent synthetic
# data.  Tests need the fallback so external services (FHIR, NPPES, OIG)
# aren't required.

@pytest.fixture(autouse=True, scope="session")
def _enable_mock_fallback():
    """Allow mock/fallback data during tests (default is disabled in production)."""
    import app.config
    original = app.config.settings.allow_mock_fallback
    app.config.settings.allow_mock_fallback = True
    yield
    app.config.settings.allow_mock_fallback = original


# ── Reset circuit breakers between tests ──────────────────────────────
# Module-level circuit breaker singletons (availity, claim_md) retain state
# across tests.  Reset them so a failure in one test doesn't cascade.

@pytest.fixture(autouse=True)
def _reset_circuit_breakers():
    """Reset all module-level circuit breakers to CLOSED before each test.

    Wrapped in try/except so that tests which don't depend on clearinghouse
    modules (e.g. Alembic migration tests) are never blocked by import
    failures in those modules.
    """
    try:
        from app.core.clearinghouse.availity import _availity_breaker
        from app.core.clearinghouse.claim_md import _claim_md_breaker
        _availity_breaker.reset()
        _claim_md_breaker.reset()
    except Exception:
        pass
    yield
    try:
        from app.core.clearinghouse.availity import _availity_breaker
        from app.core.clearinghouse.claim_md import _claim_md_breaker
        _availity_breaker.reset()
        _claim_md_breaker.reset()
    except Exception:
        pass


# ── Helper fixtures for creating test data ──────────────────────────


@pytest.fixture
def sample_org_data() -> dict:
    return {
        "name": "Test Health System",
        "npi": "1234567890",
        "tax_id": "12-3456789",
    }


@pytest.fixture
def sample_patient_data() -> dict:
    return {
        "mrn": "MRN-001",
        "first_name": "Jane",
        "last_name": "Doe",
        "date_of_birth": date(1985, 6, 15),
        "gender": "female",
        "insurance_member_id": "INS-12345",
    }
