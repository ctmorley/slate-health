"""Unit tests for JWT creation, verification, expiry, and RBAC middleware."""

import uuid
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.auth.jwt import (
    InvalidTokenError,
    TokenExpiredError,
    TokenPayload,
    create_access_token,
    create_refresh_token,
    verify_refresh_token,
    verify_token,
)

SECRET = "test-secret-key-for-jwt-at-least-32b"
ALGORITHM = "HS256"


# ── JWT Token Tests ──────────────────────────────────────────────────


def test_create_and_verify_access_token():
    """Create an access token and verify its claims."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()

    token = create_access_token(
        user_id=user_id,
        email="test@example.com",
        role="admin",
        organization_id=org_id,
        full_name="Test User",
        secret_key=SECRET,
    )

    payload = verify_token(token, secret_key=SECRET)

    assert payload.user_id == user_id
    assert payload.email == "test@example.com"
    assert payload.role == "admin"
    assert payload.organization_id == org_id
    assert payload.full_name == "Test User"
    assert payload.jti is not None


def test_token_expiry():
    """Expired tokens are rejected with TokenExpiredError."""
    token = create_access_token(
        user_id=uuid.uuid4(),
        email="test@example.com",
        role="viewer",
        expires_delta=timedelta(seconds=-1),  # Already expired
        secret_key=SECRET,
    )

    with pytest.raises(TokenExpiredError, match="expired"):
        verify_token(token, secret_key=SECRET)


def test_invalid_signature():
    """Tokens signed with wrong key are rejected."""
    token = create_access_token(
        user_id=uuid.uuid4(),
        email="test@example.com",
        role="viewer",
        secret_key="correct-key-that-is-at-least-32-bytes!",
    )

    with pytest.raises(InvalidTokenError, match="Invalid"):
        verify_token(token, secret_key="wrong-key-but-also-at-least-32-bytes!")


def test_malformed_token():
    """Malformed token strings are rejected."""
    with pytest.raises(InvalidTokenError):
        verify_token("not.a.valid.jwt.token", secret_key=SECRET)


def test_token_without_sub_claim():
    """Token without 'sub' claim is rejected."""
    import jwt as pyjwt

    bad_token = pyjwt.encode(
        {"email": "test@example.com", "role": "viewer", "exp": 9999999999},
        SECRET,
        algorithm=ALGORITHM,
    )

    with pytest.raises(InvalidTokenError, match="sub"):
        verify_token(bad_token, secret_key=SECRET)


def test_create_and_verify_refresh_token():
    """Create a refresh token and verify it."""
    user_id = uuid.uuid4()
    token = create_refresh_token(user_id=user_id, secret_key=SECRET)

    data = verify_refresh_token(token, secret_key=SECRET)

    assert data["sub"] == str(user_id)
    assert data["type"] == "refresh"
    assert "jti" in data


def test_refresh_token_expiry():
    """Expired refresh tokens are rejected."""
    token = create_refresh_token(
        user_id=uuid.uuid4(),
        secret_key=SECRET,
        expires_delta=timedelta(seconds=-1),
    )

    with pytest.raises(TokenExpiredError, match="expired"):
        verify_refresh_token(token, secret_key=SECRET)


def test_access_token_rejected_as_refresh():
    """Access tokens cannot be used as refresh tokens."""
    token = create_access_token(
        user_id=uuid.uuid4(),
        email="test@example.com",
        role="viewer",
        secret_key=SECRET,
    )

    with pytest.raises(InvalidTokenError, match="not a refresh"):
        verify_refresh_token(token, secret_key=SECRET)


def test_token_payload_to_dict():
    """TokenPayload serializes correctly."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    payload = TokenPayload(
        user_id=user_id,
        email="test@example.com",
        role="admin",
        organization_id=org_id,
    )
    data = payload.to_dict()

    assert data["sub"] == str(user_id)
    assert data["email"] == "test@example.com"
    assert data["role"] == "admin"
    assert data["org_id"] == str(org_id)


def test_token_with_no_org_id():
    """Token without org_id works correctly."""
    token = create_access_token(
        user_id=uuid.uuid4(),
        email="test@example.com",
        role="viewer",
        secret_key=SECRET,
    )

    payload = verify_token(token, secret_key=SECRET)
    assert payload.organization_id is None


# ── RBAC Integration Tests ───────────────────────────────────────────


@pytest.fixture
def admin_token():
    """JWT token with admin role."""
    return create_access_token(
        user_id=uuid.uuid4(),
        email="admin@example.com",
        role="admin",
        secret_key="change-me-in-production-use-at-least-32-bytes!",  # matches default settings.secret_key
    )


@pytest.fixture
def reviewer_token():
    """JWT token with reviewer role."""
    return create_access_token(
        user_id=uuid.uuid4(),
        email="reviewer@example.com",
        role="reviewer",
        secret_key="change-me-in-production-use-at-least-32-bytes!",
    )


@pytest.fixture
def viewer_token():
    """JWT token with viewer role."""
    return create_access_token(
        user_id=uuid.uuid4(),
        email="viewer@example.com",
        role="viewer",
        secret_key="change-me-in-production-use-at-least-32-bytes!",
    )


@pytest.fixture
def rbac_app():
    """Create a FastAPI app with RBAC-protected routes for testing."""
    from fastapi import FastAPI, Depends
    from app.core.auth.middleware import get_current_user, require_role, TokenPayload

    app = FastAPI()

    @app.get("/public")
    async def public_route():
        return {"message": "public"}

    @app.get("/authenticated")
    async def auth_route(user: TokenPayload = Depends(get_current_user)):
        return {"email": user.email, "role": user.role}

    @app.get("/admin-only")
    async def admin_route(user: TokenPayload = Depends(require_role("admin"))):
        return {"message": "admin access granted", "email": user.email}

    @app.get("/reviewer-only")
    async def reviewer_route(user: TokenPayload = Depends(require_role("reviewer"))):
        return {"message": "reviewer access granted"}

    return app


@pytest.mark.asyncio
async def test_unauthenticated_request_returns_401(rbac_app):
    """Authenticated endpoint returns 401 without token."""
    transport = ASGITransport(app=rbac_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get("/authenticated")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_valid_token_returns_200(rbac_app, admin_token):
    """Authenticated endpoint returns 200 with valid token."""
    transport = ASGITransport(app=rbac_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get(
            "/authenticated",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert response.status_code == 200
    assert response.json()["email"] == "admin@example.com"


@pytest.mark.asyncio
async def test_expired_token_returns_401(rbac_app):
    """Expired token returns 401."""
    expired = create_access_token(
        user_id=uuid.uuid4(),
        email="expired@example.com",
        role="admin",
        expires_delta=timedelta(seconds=-1),
        secret_key="change-me-in-production-use-at-least-32-bytes!",
    )
    transport = ASGITransport(app=rbac_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get(
            "/authenticated",
            headers={"Authorization": f"Bearer {expired}"},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_admin_route_allows_admin(rbac_app, admin_token):
    """Admin route allows admin users."""
    transport = ASGITransport(app=rbac_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get(
            "/admin-only",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert response.status_code == 200
    assert response.json()["message"] == "admin access granted"


@pytest.mark.asyncio
async def test_admin_route_rejects_viewer(rbac_app, viewer_token):
    """Admin route returns 403 for viewer-role users."""
    transport = ASGITransport(app=rbac_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get(
            "/admin-only",
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_route_rejects_reviewer(rbac_app, reviewer_token):
    """Admin route returns 403 for reviewer-role users."""
    transport = ASGITransport(app=rbac_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get(
            "/admin-only",
            headers={"Authorization": f"Bearer {reviewer_token}"},
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_reviewer_route_allows_admin(rbac_app, admin_token):
    """Reviewer route allows admin users (higher role)."""
    transport = ASGITransport(app=rbac_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get(
            "/reviewer-only",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_reviewer_route_allows_reviewer(rbac_app, reviewer_token):
    """Reviewer route allows reviewer users."""
    transport = ASGITransport(app=rbac_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get(
            "/reviewer-only",
            headers={"Authorization": f"Bearer {reviewer_token}"},
        )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_reviewer_route_rejects_viewer(rbac_app, viewer_token):
    """Reviewer route returns 403 for viewer-role users."""
    transport = ASGITransport(app=rbac_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get(
            "/reviewer-only",
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
    assert response.status_code == 403
