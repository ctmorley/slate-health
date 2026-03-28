"""JWT token creation, verification, and user/role extraction.

Uses PyJWT (via python-jose or stdlib) for HS256 token handling.
Tokens carry user_id, email, role, and org_id claims.
"""

from __future__ import annotations

import uuid
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt as pyjwt

from app.config import settings

logger = logging.getLogger(__name__)


class JWTError(Exception):
    """Raised for JWT-related errors (expired, invalid, malformed)."""
    pass


class TokenExpiredError(JWTError):
    """Raised when a JWT token has expired."""
    pass


class InvalidTokenError(JWTError):
    """Raised when a JWT token is invalid (bad signature, malformed)."""
    pass


# ── Token Payload Model ──────────────────────────────────────────────


class TokenPayload:
    """Decoded JWT token payload with typed fields."""

    def __init__(
        self,
        user_id: uuid.UUID,
        email: str,
        role: str,
        organization_id: uuid.UUID | None = None,
        full_name: str = "",
        exp: datetime | None = None,
        iat: datetime | None = None,
        jti: str | None = None,
    ) -> None:
        self.user_id = user_id
        self.email = email
        self.role = role
        self.organization_id = organization_id
        self.full_name = full_name
        self.exp = exp
        self.iat = iat
        self.jti = jti

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for JWT encoding."""
        payload: dict[str, Any] = {
            "sub": str(self.user_id),
            "email": self.email,
            "role": self.role,
            "full_name": self.full_name,
        }
        if self.organization_id:
            payload["org_id"] = str(self.organization_id)
        if self.exp:
            payload["exp"] = self.exp
        if self.iat:
            payload["iat"] = self.iat
        if self.jti:
            payload["jti"] = self.jti
        return payload


# ── Token Operations ─────────────────────────────────────────────────


def create_access_token(
    *,
    user_id: uuid.UUID,
    email: str,
    role: str,
    organization_id: uuid.UUID | None = None,
    full_name: str = "",
    expires_delta: timedelta | None = None,
    secret_key: str | None = None,
) -> str:
    """Create a signed JWT access token.

    Args:
        user_id: User's UUID.
        email: User's email address.
        role: User's role (admin, reviewer, viewer).
        organization_id: User's organization UUID (optional).
        full_name: User's display name.
        expires_delta: Custom expiry duration. Defaults to settings.jwt_expiry_minutes.
        secret_key: Override for signing key. Defaults to settings.secret_key.

    Returns:
        Encoded JWT string.
    """
    now = datetime.now(timezone.utc)
    if expires_delta is None:
        expires_delta = timedelta(minutes=settings.jwt_expiry_minutes)

    payload = TokenPayload(
        user_id=user_id,
        email=email,
        role=role,
        organization_id=organization_id,
        full_name=full_name,
        exp=now + expires_delta,
        iat=now,
        jti=str(uuid.uuid4()),
    )

    key = secret_key or settings.secret_key
    token = pyjwt.encode(
        payload.to_dict(),
        key,
        algorithm=settings.jwt_algorithm,
    )
    return token


def create_refresh_token(
    *,
    user_id: uuid.UUID,
    secret_key: str | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    """Create a refresh token with longer expiry.

    Refresh tokens carry only the user_id and a unique jti for rotation tracking.
    """
    now = datetime.now(timezone.utc)
    if expires_delta is None:
        expires_delta = timedelta(days=7)

    payload = {
        "sub": str(user_id),
        "type": "refresh",
        "exp": now + expires_delta,
        "iat": now,
        "jti": str(uuid.uuid4()),
    }

    key = secret_key or settings.secret_key
    return pyjwt.encode(payload, key, algorithm=settings.jwt_algorithm)


def verify_token(
    token: str,
    *,
    secret_key: str | None = None,
) -> TokenPayload:
    """Verify and decode a JWT access token.

    Args:
        token: Encoded JWT string.
        secret_key: Override for verification key.

    Returns:
        Decoded TokenPayload.

    Raises:
        TokenExpiredError: If the token has expired.
        InvalidTokenError: If the token is invalid or malformed.
    """
    key = secret_key or settings.secret_key
    try:
        data = pyjwt.decode(
            token,
            key,
            algorithms=[settings.jwt_algorithm],
        )
    except pyjwt.ExpiredSignatureError:
        raise TokenExpiredError("Token has expired")
    except pyjwt.InvalidTokenError as exc:
        raise InvalidTokenError(f"Invalid token: {exc}")

    try:
        user_id = uuid.UUID(data["sub"])
    except (KeyError, ValueError) as exc:
        raise InvalidTokenError(f"Token missing or invalid 'sub' claim: {exc}")

    org_id = None
    if "org_id" in data:
        try:
            org_id = uuid.UUID(data["org_id"])
        except ValueError:
            pass

    return TokenPayload(
        user_id=user_id,
        email=data.get("email", ""),
        role=data.get("role", "viewer"),
        organization_id=org_id,
        full_name=data.get("full_name", ""),
        exp=datetime.fromtimestamp(data["exp"], tz=timezone.utc) if "exp" in data else None,
        iat=datetime.fromtimestamp(data["iat"], tz=timezone.utc) if "iat" in data else None,
        jti=data.get("jti"),
    )


def verify_refresh_token(
    token: str,
    *,
    secret_key: str | None = None,
) -> dict[str, Any]:
    """Verify a refresh token and return its claims.

    Returns:
        Dictionary with 'sub' (user_id str), 'jti', 'type'.

    Raises:
        TokenExpiredError: If expired.
        InvalidTokenError: If invalid.
    """
    key = secret_key or settings.secret_key
    try:
        data = pyjwt.decode(token, key, algorithms=[settings.jwt_algorithm])
    except pyjwt.ExpiredSignatureError:
        raise TokenExpiredError("Refresh token has expired")
    except pyjwt.InvalidTokenError as exc:
        raise InvalidTokenError(f"Invalid refresh token: {exc}")

    if data.get("type") != "refresh":
        raise InvalidTokenError("Token is not a refresh token")

    # Validate sub claim is present and is a valid UUID format
    sub = data.get("sub")
    if not sub:
        raise InvalidTokenError("Refresh token missing 'sub' claim")
    try:
        uuid.UUID(sub)
    except (ValueError, AttributeError):
        raise InvalidTokenError(f"Refresh token 'sub' claim is not a valid UUID: {sub!r}")

    # Require JTI for revocation tracking — tokens without JTI cannot be
    # reliably revoked, so reject them to close the bypass vector.
    jti = data.get("jti")
    if not jti or not isinstance(jti, str) or not jti.strip():
        raise InvalidTokenError(
            "Refresh token missing or empty 'jti' claim (required for revocation tracking)"
        )

    return data
