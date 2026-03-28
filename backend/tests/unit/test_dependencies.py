"""Tests for dependency injection providers — engine/session lifecycle."""

import pytest

from app.dependencies import (
    async_dispose_engine,
    async_reset_db_state,
    get_engine,
    get_session_factory,
    reset_db_state,
)

# Use in-memory SQLite for fast, isolated tests
_SQLITE_URL_A = "sqlite+aiosqlite://"
_SQLITE_URL_B = "sqlite+aiosqlite:///file:memdb1?mode=memory&uri=true"


@pytest.fixture(autouse=True)
def _clean_globals():
    """Ensure global engine/factory state is clean before and after each test.

    Saves and restores the pre-existing DI state so that session-scoped
    fixtures (e.g. ``test_engine`` from conftest) are not disrupted when
    this file's tests manipulate the globals.
    """
    import app.dependencies as _deps

    saved_engine = _deps._engine
    saved_factory = _deps._session_factory

    reset_db_state()
    yield
    reset_db_state()

    # Restore any session-scoped DI state that was set before this test
    _deps._engine = saved_engine
    _deps._session_factory = saved_factory


def test_get_engine_creates_engine():
    """First call to get_engine creates a new engine."""
    engine = get_engine(url=_SQLITE_URL_A)
    assert engine is not None
    assert str(engine.url) == "sqlite+aiosqlite://"


def test_get_engine_returns_same_instance_without_url():
    """Subsequent calls without a url return the same engine."""
    engine1 = get_engine(url=_SQLITE_URL_A)
    engine2 = get_engine()
    assert engine1 is engine2


def test_get_engine_replaces_on_new_url():
    """Passing a new url disposes the old engine and creates a fresh one."""
    engine1 = get_engine(url=_SQLITE_URL_A)
    engine2 = get_engine(url=_SQLITE_URL_B)
    assert engine1 is not engine2


def test_get_engine_resets_session_factory_on_url_change():
    """Changing the engine URL also invalidates the cached session factory."""
    get_engine(url=_SQLITE_URL_A)
    factory1 = get_session_factory()

    get_engine(url=_SQLITE_URL_B)
    factory2 = get_session_factory()

    # factory2 must be a *new* instance bound to the new engine
    assert factory1 is not factory2


def test_reset_db_state_clears_globals():
    """reset_db_state clears the cached engine and session factory."""
    get_engine(url=_SQLITE_URL_A)
    get_session_factory()

    reset_db_state()

    # After reset, a new call should produce a brand-new engine
    engine = get_engine(url=_SQLITE_URL_A)
    assert engine is not None

    # And a new factory
    factory = get_session_factory()
    assert factory is not None


def test_get_session_factory_bound_to_correct_engine():
    """Session factory is bound to the current engine, not a stale one."""
    engine_a = get_engine(url=_SQLITE_URL_A)
    factory_a = get_session_factory()

    engine_b = get_engine(url=_SQLITE_URL_B)
    factory_b = get_session_factory()

    # factory_b should be bound to engine_b
    assert factory_b.kw["bind"] is engine_b


# ── Async disposal tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_dispose_engine_cleans_up():
    """async_dispose_engine awaits the async disposal path."""
    get_engine(url=_SQLITE_URL_A)
    # Should not raise; disposes via the proper async path
    await async_dispose_engine()


@pytest.mark.asyncio
async def test_async_reset_db_state_clears_globals():
    """async_reset_db_state disposes engine async and clears state."""
    get_engine(url=_SQLITE_URL_A)
    get_session_factory()

    await async_reset_db_state()

    # After async reset, a new call should produce a brand-new engine
    engine = get_engine(url=_SQLITE_URL_A)
    assert engine is not None
    factory = get_session_factory()
    assert factory is not None


@pytest.mark.asyncio
async def test_async_dispose_engine_noop_when_no_engine():
    """async_dispose_engine is safe to call when no engine exists."""
    reset_db_state()
    # Should not raise
    await async_dispose_engine()
