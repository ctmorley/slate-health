"""Unit tests for structured logging with correlation IDs.

Tests cover:
- CorrelationIdMiddleware generates UUID when no header present
- CorrelationIdMiddleware uses existing X-Correlation-ID header
- get_correlation_id returns the current correlation ID
- JSON log formatter produces valid JSON with expected fields
- CorrelationIdFilter injects correlation_id into log records
"""

from __future__ import annotations

import json
import logging
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core.logging_config import (
    CorrelationIdFilter,
    CorrelationIdMiddleware,
    JSONFormatter,
    get_correlation_id,
)


# ── CorrelationIdMiddleware Tests ─────────────────────────────────────


def _make_test_app():
    """Create a minimal Starlette app for testing middleware."""

    async def index(request: Request):
        return JSONResponse({"correlation_id": get_correlation_id()})

    app = Starlette(routes=[Route("/", index)])
    app.add_middleware(CorrelationIdMiddleware)
    return app


class TestCorrelationIdMiddleware:
    @pytest.mark.asyncio
    async def test_generates_correlation_id_when_not_provided(self):
        app = _make_test_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/")

        assert resp.status_code == 200
        data = resp.json()
        # Should be a valid UUID
        cid = data["correlation_id"]
        uuid.UUID(cid)  # Raises ValueError if invalid

        # Response header should also contain the correlation ID
        assert resp.headers.get("x-correlation-id") == cid

    @pytest.mark.asyncio
    async def test_uses_existing_correlation_id_header(self):
        app = _make_test_app()
        transport = ASGITransport(app=app)
        my_id = "test-corr-id-12345"
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/", headers={"X-Correlation-ID": my_id})

        assert resp.status_code == 200
        assert resp.json()["correlation_id"] == my_id
        assert resp.headers.get("x-correlation-id") == my_id


# ── JsonFormatter Tests ───────────────────────────────────────────────


class TestJsonFormatter:
    def test_produces_valid_json(self):
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=42,
            msg="Hello %s",
            args=("world",),
            exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test.logger"
        assert parsed["message"] == "Hello world"
        assert "timestamp" in parsed

    def test_includes_exception_info(self):
        formatter = JSONFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test.logger",
            level=logging.ERROR,
            pathname="test.py",
            lineno=42,
            msg="Error occurred",
            args=(),
            exc_info=exc_info,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "exception" in parsed
        assert "ValueError" in parsed["exception"]


# ── CorrelationIdFilter Tests ─────────────────────────────────────────


class TestCorrelationIdFilter:
    def test_injects_correlation_id(self):
        filt = CorrelationIdFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO,
            pathname="", lineno=0, msg="test", args=(), exc_info=None,
        )
        filt.filter(record)
        assert hasattr(record, "correlation_id")
        # When not in a request context, should still have a value (empty string or default)
        assert isinstance(record.correlation_id, str)
