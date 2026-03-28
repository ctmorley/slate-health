"""Integration tests for RBAC middleware on production app routes.

Tests that actual routes in create_app() enforce JWT auth and role-based access.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.auth.jwt import create_access_token
from app.dependencies import get_db
from app.main import create_app


@pytest.fixture
async def unauthenticated_client(test_engine):
    """Client without any auth token, using the test database."""
    session_factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False,
    )

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _make_token(role: str = "viewer", **kwargs) -> str:
    """Helper to create a JWT token with given role."""
    return create_access_token(
        user_id=kwargs.get("user_id", uuid.uuid4()),
        email=kwargs.get("email", f"{role}@test.com"),
        role=role,
        full_name=kwargs.get("full_name", f"Test {role.capitalize()}"),
    )


# ── Public Endpoints ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_no_auth_required(unauthenticated_client):
    """GET /health does not require authentication."""
    resp = await unauthenticated_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


# ── Protected Endpoints: 401 without token ──────────────────────────


@pytest.mark.asyncio
async def test_auth_me_requires_token(unauthenticated_client):
    """GET /api/v1/auth/me returns 401 without JWT."""
    resp = await unauthenticated_client.get("/api/v1/auth/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_dashboard_requires_token(unauthenticated_client):
    """GET /api/v1/dashboard/summary returns 401 without JWT."""
    resp = await unauthenticated_client.get("/api/v1/dashboard/summary")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_settings_requires_token(unauthenticated_client):
    """GET /api/v1/admin/settings returns 401 without JWT."""
    resp = await unauthenticated_client.get("/api/v1/admin/settings")
    assert resp.status_code == 401


# ── Protected Endpoints: 200 with valid token ───────────────────────


@pytest.mark.asyncio
async def test_auth_me_with_valid_token(unauthenticated_client):
    """GET /api/v1/auth/me returns 200 with valid JWT."""
    token = _make_token("viewer")
    resp = await unauthenticated_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["roles"] == ["viewer"]
    assert data["email"] == "viewer@test.com"


@pytest.mark.asyncio
async def test_dashboard_with_viewer_token(unauthenticated_client):
    """GET /api/v1/dashboard/summary returns 200 for viewer role."""
    token = _make_token("viewer")
    resp = await unauthenticated_client.get(
        "/api/v1/dashboard/summary",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


# ── RBAC: 403 for insufficient role ─────────────────────────────────


@pytest.mark.asyncio
async def test_admin_settings_forbidden_for_viewer(unauthenticated_client):
    """GET /api/v1/admin/settings returns 403 for viewer role."""
    token = _make_token("viewer")
    resp = await unauthenticated_client.get(
        "/api/v1/admin/settings",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    assert "Insufficient permissions" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_admin_settings_forbidden_for_reviewer(unauthenticated_client):
    """GET /api/v1/admin/settings returns 403 for reviewer role."""
    token = _make_token("reviewer")
    resp = await unauthenticated_client.get(
        "/api/v1/admin/settings",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


# ── RBAC: 200 for sufficient role ───────────────────────────────────


@pytest.mark.asyncio
async def test_admin_settings_allowed_for_admin(unauthenticated_client):
    """GET /api/v1/admin/settings returns 200 for admin role."""
    token = _make_token("admin")
    resp = await unauthenticated_client.get(
        "/api/v1/admin/settings",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_dashboard_allowed_for_admin(unauthenticated_client):
    """GET /api/v1/dashboard/summary returns 200 for admin role (role hierarchy)."""
    token = _make_token("admin")
    resp = await unauthenticated_client.get(
        "/api/v1/dashboard/summary",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_expired_token_returns_401(unauthenticated_client):
    """Expired JWT returns 401."""
    from datetime import timedelta
    token = create_access_token(
        user_id=uuid.uuid4(),
        email="expired@test.com",
        role="admin",
        expires_delta=timedelta(seconds=-10),
    )
    resp = await unauthenticated_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_invalid_token_returns_401(unauthenticated_client):
    """Invalid/malformed JWT returns 401."""
    resp = await unauthenticated_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer invalid.token.here"},
    )
    assert resp.status_code == 401
