"""Tests for the health check and readiness endpoints."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_returns_200(client: AsyncClient):
    """GET /health returns 200 with healthy status."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


@pytest.mark.asyncio
async def test_health_response_content_type(client: AsyncClient):
    """Health endpoint returns JSON content type."""
    response = await client.get("/health")
    assert "application/json" in response.headers["content-type"]


@pytest.mark.asyncio
async def test_ready_returns_200_when_db_connected(client: AsyncClient):
    """GET /ready returns 200 with database connected status when DB is available."""
    response = await client.get("/ready")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["database"] == "connected"


@pytest.mark.asyncio
async def test_ready_returns_503_when_db_unavailable():
    """GET /ready returns 503 when database is not reachable."""
    from contextlib import asynccontextmanager
    from unittest.mock import patch

    from httpx import ASGITransport, AsyncClient as HttpxAsyncClient

    from app.main import create_app

    app = create_app()

    # Create a factory whose sessions always fail on execute
    class _FailSession:
        async def execute(self, stmt):
            raise ConnectionRefusedError("DB unavailable")

    @asynccontextmanager
    async def _fail_ctx():
        yield _FailSession()

    def _broken_factory():
        class _F:
            def __call__(self):
                return _fail_ctx()
        return _F()

    with patch("app.main.get_session_factory", _broken_factory):
        transport = ASGITransport(app=app)
        async with HttpxAsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.get("/ready")
            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "not_ready"
            assert data["database"] == "disconnected"
            # Verify no internal exception details are leaked to the client
            assert "detail" not in data
