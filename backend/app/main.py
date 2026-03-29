"""FastAPI application factory for Slate Health."""

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import Response
from sqlalchemy import text

from app.config import settings
from app.core.auth.jwt import TokenPayload
from app.core.auth.middleware import (
    CurrentUser,
    LoginRedirectMiddleware,
    get_current_user,
    require_role,
)
from app.core.logging_config import (
    CORRELATION_ID_HEADER,
    CorrelationIdMiddleware,
    get_correlation_id,
    setup_logging,
)
from app.core.rate_limiter import RateLimitMiddleware
from app.dependencies import async_dispose_engine, get_engine, get_session_factory

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: startup and shutdown events."""
    get_engine()  # ensure engine is initialized at startup
    yield
    await async_dispose_engine()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""

    # Configure structured logging before anything else
    setup_logging(level=settings.log_level, log_format=settings.log_format)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )

    # ── Public endpoints (no auth required) ──────────────────────────

    @app.get("/health")
    async def health_check() -> dict:
        """Liveness probe — always returns healthy if the process is up."""
        return {"status": "healthy"}

    @app.get("/ready")
    async def readiness_check() -> dict:
        """Readiness probe — verifies database connectivity."""
        try:
            factory = get_session_factory()
            async with factory() as session:
                await session.execute(text("SELECT 1"))
            return {"status": "ready", "database": "connected"}
        except Exception as exc:
            from fastapi.responses import JSONResponse

            logger.error("Readiness check failed: %s", exc, exc_info=True)
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "database": "disconnected",
                },
            )

    # ── API v1 routes ────────────────────────────────────────────────

    from app.api.v1.router import api_router
    from app.api.websocket import router as ws_router

    # ── Middleware (outermost first — Starlette processes them in
    #    reverse registration order, so the last added runs first) ────

    # Rate limiting (runs early, before auth)
    # Uses Redis-backed counter when SLATE_RATE_LIMIT_REDIS_URL is set
    # (required for multi-replica production deployments).
    app.add_middleware(
        RateLimitMiddleware,
        max_requests=settings.rate_limit_max_requests,
        window_seconds=settings.rate_limit_window_seconds,
        redis_url=settings.rate_limit_redis_url or None,
    )

    # Add login redirect middleware for unauthenticated browser requests
    app.add_middleware(LoginRedirectMiddleware)

    # CORS — allow frontend origins
    cors_origins = [
        o.strip()
        for o in (settings.cors_origins or "").split(",")
        if o.strip()
    ]
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Correlation ID tracking (runs first — outermost middleware)
    app.add_middleware(CorrelationIdMiddleware)

    # Response middleware: propagate correlation ID to all responses
    @app.middleware("http")
    async def add_correlation_id_header(request: Request, call_next):  # type: ignore[no-untyped-def]
        response: Response = await call_next(request)
        cid = get_correlation_id()
        if cid:
            response.headers[CORRELATION_ID_HEADER] = cid
        return response

    app.include_router(api_router)
    app.include_router(ws_router)

    # ── Legacy protected endpoints (kept for backward compatibility) ──

    @app.get("/api/v1/admin/settings")
    async def admin_settings(
        current_user: TokenPayload = Depends(require_role("admin")),
    ) -> dict:
        """Admin settings — requires admin role."""
        return {
            "status": "ok",
            "admin_user": str(current_user.user_id),
        }

    return app


app = create_app()
