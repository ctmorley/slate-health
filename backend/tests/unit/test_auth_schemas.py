"""Unit tests for authentication Pydantic schemas."""

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.auth import (
    AuthError,
    LoginRequest,
    LoginResponse,
    RefreshRequest,
    TokenResponse,
    UserProfile,
)


# ── LoginRequest ────────────────────────────────────────────────────


def test_login_request_valid():
    """LoginRequest accepts valid provider values."""
    req = LoginRequest(provider="saml")
    assert req.provider == "saml"
    assert req.redirect_url is None


def test_login_request_with_redirect():
    """LoginRequest accepts optional redirect_url."""
    req = LoginRequest(provider="oidc", redirect_url="https://app.example.com/callback")
    assert req.provider == "oidc"
    assert req.redirect_url == "https://app.example.com/callback"


def test_login_request_missing_provider():
    """LoginRequest requires provider field."""
    with pytest.raises(ValidationError):
        LoginRequest()


# ── LoginResponse ──────────────────────────────────────────────────


def test_login_response_valid():
    """LoginResponse contains redirect URL and provider."""
    resp = LoginResponse(redirect_url="https://idp.example.com/auth", provider="saml")
    assert resp.redirect_url == "https://idp.example.com/auth"
    assert resp.provider == "saml"


# ── TokenResponse ─────────────────────────────────────────────────


def test_token_response_valid():
    """TokenResponse contains access and refresh tokens."""
    resp = TokenResponse(
        access_token="eyJ...",
        refresh_token="eyR...",
        expires_in=3600,
    )
    assert resp.access_token == "eyJ..."
    assert resp.refresh_token == "eyR..."
    assert resp.token_type == "bearer"
    assert resp.expires_in == 3600


def test_token_response_missing_fields():
    """TokenResponse requires access_token, refresh_token, expires_in."""
    with pytest.raises(ValidationError):
        TokenResponse(access_token="x")


# ── RefreshRequest ────────────────────────────────────────────────


def test_refresh_request_valid():
    """RefreshRequest accepts refresh token string."""
    req = RefreshRequest(refresh_token="eyR...")
    assert req.refresh_token == "eyR..."


def test_refresh_request_missing():
    """RefreshRequest requires refresh_token."""
    with pytest.raises(ValidationError):
        RefreshRequest()


# ── UserProfile ───────────────────────────────────────────────────


def test_user_profile_valid():
    """UserProfile contains all user fields."""
    uid = uuid.uuid4()
    org_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    profile = UserProfile(
        id=uid,
        email="jane@example.com",
        full_name="Jane Doe",
        roles=["admin"],
        organization_id=org_id,
        last_login=now,
        is_active=True,
    )
    assert profile.id == uid
    assert profile.email == "jane@example.com"
    assert profile.roles == ["admin"]
    assert profile.organization_id == org_id
    assert profile.is_active is True


def test_user_profile_minimal():
    """UserProfile works with only required fields."""
    uid = uuid.uuid4()
    profile = UserProfile(
        id=uid,
        email="bob@example.com",
        full_name="Bob Smith",
        roles=["viewer"],
    )
    assert profile.organization_id is None
    assert profile.last_login is None
    assert profile.is_active is True


def test_user_profile_from_attributes():
    """UserProfile supports from_attributes mode for ORM compatibility."""
    uid = uuid.uuid4()
    data = {
        "id": uid,
        "email": "test@example.com",
        "full_name": "Test User",
        "roles": ["reviewer"],
        "is_active": True,
    }
    profile = UserProfile.model_validate(data)
    assert profile.email == "test@example.com"


# ── AuthError ─────────────────────────────────────────────────────


def test_auth_error_valid():
    """AuthError contains detail and optional error_code."""
    err = AuthError(detail="Invalid credentials", error_code="AUTH_FAILED")
    assert err.detail == "Invalid credentials"
    assert err.error_code == "AUTH_FAILED"


def test_auth_error_no_code():
    """AuthError works without error_code."""
    err = AuthError(detail="Unauthorized")
    assert err.detail == "Unauthorized"
    assert err.error_code is None
