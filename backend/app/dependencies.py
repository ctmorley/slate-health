"""Dependency injection providers for database sessions and services."""

from __future__ import annotations

import logging
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# ── Temporal Client Singleton ────────────────────────────────────────

_temporal_client: object | None = None
_temporal_client_initialized: bool = False


async def get_temporal_client():
    """Get or create the Temporal client singleton.

    Returns None if the Temporal server is not reachable, allowing the
    system to fall back to inline execution.
    """
    global _temporal_client, _temporal_client_initialized

    if _temporal_client_initialized:
        return _temporal_client

    try:
        from temporalio.client import Client as TemporalClient

        target = settings.temporal_address
        _temporal_client = await TemporalClient.connect(target)
        _temporal_client_initialized = True
        logger.info("Connected to Temporal at %s", target)
    except Exception as exc:
        logger.warning(
            "Temporal not available (%s); workflows will run inline. "
            "Will retry connection on next request.",
            exc,
        )
        _temporal_client = None
        # Do NOT set _temporal_client_initialized — allow retry on next call

    return _temporal_client


def set_temporal_client(client: object | None) -> None:
    """Override the Temporal client (used in tests and DI)."""
    global _temporal_client, _temporal_client_initialized
    _temporal_client = client
    _temporal_client_initialized = True


def reset_temporal_client() -> None:
    """Reset the Temporal client state (for tests)."""
    global _temporal_client, _temporal_client_initialized
    _temporal_client = None
    _temporal_client_initialized = False


def get_engine(url: str | None = None) -> AsyncEngine:
    """Get or create the async SQLAlchemy engine.

    When a new *url* is supplied and an engine already exists, the previous
    engine is disposed synchronously (via the underlying sync engine) and
    the session factory is reset so that callers always get a factory bound
    to the current engine.

    For proper async cleanup prefer :func:`async_dispose_engine` or
    :func:`async_reset_db_state` which await the async disposal path.
    This synchronous disposal is a fallback for contexts where an event
    loop is not running (e.g. module teardown).
    """
    global _engine, _session_factory
    if _engine is None or url is not None:
        if _engine is not None:
            # Synchronous fallback: dispose via underlying sync engine.
            _engine.sync_engine.dispose()
            _session_factory = None  # force re-creation on next access
        db_url = url or settings.database_url

        engine_kwargs: dict = {
            "echo": settings.debug,
            "pool_pre_ping": True,
        }

        # Connection-pool tuning only applies to pool-capable backends
        # (PostgreSQL).  SQLite uses StaticPool / NullPool and does not
        # accept these parameters.
        if "postgresql" in db_url:
            engine_kwargs.update(
                {
                    "pool_size": settings.db_pool_size,
                    "max_overflow": settings.db_max_overflow,
                    "pool_timeout": settings.db_pool_timeout,
                    "pool_recycle": settings.db_pool_recycle,
                }
            )

        _engine = create_async_engine(db_url, **engine_kwargs)
    return _engine


def get_session_factory(engine: AsyncEngine | None = None) -> async_sessionmaker[AsyncSession]:
    """Get or create the async session factory.

    If a new *engine* is supplied the factory is rebuilt to bind to it,
    and any previous factory is discarded.
    """
    global _session_factory
    if _session_factory is None or engine is not None:
        eng = engine or get_engine()
        _session_factory = async_sessionmaker(
            eng,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


async def async_dispose_engine() -> None:
    """Dispose the current engine using the proper async path.

    This should be used in application shutdown hooks and async test
    teardown to cleanly release all pooled connections.
    """
    global _engine
    if _engine is not None:
        await _engine.dispose()


async def async_reset_db_state() -> None:
    """Async version of :func:`reset_db_state`.

    Awaits the engine's async disposal to ensure all pooled connections
    are cleanly released before clearing state.  Prefer this over
    :func:`reset_db_state` in any async context (tests, shutdown hooks).
    """
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def reset_db_state() -> None:
    """Reset the global engine and session factory (synchronous fallback).

    Uses the synchronous disposal path via the underlying sync engine.
    Prefer :func:`async_reset_db_state` when running inside an async
    context to ensure proper connection cleanup.
    """
    global _engine, _session_factory
    if _engine is not None:
        _engine.sync_engine.dispose()
    _engine = None
    _session_factory = None


def create_disposable_engine(url: str | None = None) -> AsyncEngine:
    """Create a disposable async engine with the same pool tuning as the main engine.

    Used in Temporal activity workers and background awaiter tasks that run
    outside the FastAPI DI lifecycle but still need properly-tuned DB access.

    The caller is responsible for disposing the engine when done::

        engine = create_disposable_engine()
        try:
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session:
                ...
        finally:
            await engine.dispose()
    """
    db_url = url or settings.database_url

    engine_kwargs: dict = {
        "echo": settings.debug,
        "pool_pre_ping": True,
    }

    if "postgresql" in db_url:
        engine_kwargs.update(
            {
                "pool_size": settings.db_pool_size,
                "max_overflow": settings.db_max_overflow,
                "pool_timeout": settings.db_pool_timeout,
                "pool_recycle": settings.db_pool_recycle,
            }
        )

    return create_async_engine(db_url, **engine_kwargs)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
