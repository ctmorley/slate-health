"""Pydantic schemas for authentication request/response models."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    """Request to initiate SSO login flow."""
    provider: str = Field(description="SSO provider: 'saml' or 'oidc'")
    redirect_url: str | None = Field(
        default=None,
        description="URL to redirect after successful authentication",
    )


class LoginResponse(BaseModel):
    """Response with SSO redirect URL."""
    redirect_url: str = Field(description="URL to redirect the user to for SSO authentication")
    provider: str


class TokenResponse(BaseModel):
    """JWT token pair response."""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access token expiry in seconds")


class RefreshRequest(BaseModel):
    """Request to refresh an access token."""
    refresh_token: str


class UserProfile(BaseModel):
    """Current user profile response."""
    id: uuid.UUID
    email: str
    full_name: str
    roles: list[str] = Field(
        description="User's roles (currently single-role, returned as list for forward compatibility)"
    )
    organization_id: uuid.UUID | None = None
    last_login: datetime | None = None
    is_active: bool = True

    model_config = {"from_attributes": True}


class LoginPageResponse(BaseModel):
    """Response for GET /login — lists available SSO providers."""
    message: str = "SSO login required"
    providers: list[str] = Field(description="Available SSO providers (e.g. 'saml', 'oidc')")
    redirect_url: str = Field(description="Post-authentication redirect URL")
    login_endpoint: str = "/api/v1/auth/login"
    usage: str = Field(
        default="POST to /api/v1/auth/login with {\"provider\": \"saml\" | \"oidc\"} to initiate SSO flow",
        description="Usage instructions",
    )


class AuthError(BaseModel):
    """Authentication error response."""
    detail: str
    error_code: str | None = None
