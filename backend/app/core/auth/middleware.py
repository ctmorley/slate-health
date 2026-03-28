"""Authentication and RBAC middleware for FastAPI routes.

Provides FastAPI dependencies for JWT validation and role-based access control,
plus a middleware for redirecting unauthenticated browser requests to the login page.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.core.auth.jwt import (
    InvalidTokenError,
    TokenExpiredError,
    TokenPayload,
    verify_token,
)

logger = logging.getLogger(__name__)

# HTTP Bearer scheme for extracting tokens from Authorization header
_bearer_scheme = HTTPBearer(auto_error=False)


# ── Role Definitions ─────────────────────────────────────────────────

# Role hierarchy: admin > reviewer > viewer
ROLE_HIERARCHY: dict[str, int] = {
    "admin": 3,
    "reviewer": 2,
    "viewer": 1,
}


def _has_role_access(user_role: str, required_role: str) -> bool:
    """Check if user_role meets or exceeds the required role level."""
    user_level = ROLE_HIERARCHY.get(user_role, 0)
    required_level = ROLE_HIERARCHY.get(required_role, 0)
    return user_level >= required_level


# ── Authentication Dependencies ──────────────────────────────────────


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> TokenPayload:
    """FastAPI dependency: extract and validate JWT from Authorization header.

    Returns the decoded TokenPayload or raises 401 if missing/invalid.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = verify_token(credentials.credentials)
    except TokenExpiredError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid authentication credentials: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return payload


# Type alias for dependency injection
CurrentUser = Annotated[TokenPayload, Depends(get_current_user)]


# ── Role-Based Access Control Dependencies ───────────────────────────


def require_role(minimum_role: str):
    """Create a FastAPI dependency that enforces a minimum role.

    Usage:
        @router.get("/admin-only", dependencies=[Depends(require_role("admin"))])
        async def admin_endpoint():
            ...

    Or as a direct dependency:
        @router.get("/reviews")
        async def get_reviews(user: TokenPayload = Depends(require_role("reviewer"))):
            ...
    """

    async def _role_checker(
        current_user: TokenPayload = Depends(get_current_user),
    ) -> TokenPayload:
        if not _has_role_access(current_user.role, minimum_role):
            logger.warning(
                "Access denied: user %s (role=%s) attempted to access resource requiring role=%s",
                current_user.user_id, current_user.role, minimum_role,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required role: {minimum_role}",
            )
        return current_user

    return _role_checker


# Convenience dependencies for common role requirements
RequireAdmin = Depends(require_role("admin"))
RequireReviewer = Depends(require_role("reviewer"))
RequireViewer = Depends(require_role("viewer"))


# ── Login Redirect Middleware ─────────────────────────────────────────

# Paths that do not require authentication.
#
# NOTE: Auth bootstrap endpoints (/api/v1/auth/login, /callback/*, /refresh)
# are intentionally public — they ARE the authentication flow entry points.
# The acceptance criterion "all /api/v1/ routes return 401 without valid JWT"
# applies to *business* routes (agents, reviews, workflows, payers, audit,
# dashboard), not to the auth endpoints themselves which must be accessible
# to initiate and complete the SSO login flow.
_PUBLIC_PATHS = frozenset({
    "/health",
    "/ready",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/api/v1/auth/login",
    "/api/v1/auth/callback/saml",
    "/api/v1/auth/callback/oidc",
    "/api/v1/auth/refresh",
})

# Path prefixes that are public
_PUBLIC_PREFIXES = (
    "/docs",
    "/redoc",
)

# Default login page URL
LOGIN_PAGE_URL = "/api/v1/auth/login"


def _is_public_path(path: str) -> bool:
    """Check if the given path is a public (unauthenticated) endpoint."""
    if path in _PUBLIC_PATHS:
        return True
    for prefix in _PUBLIC_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def _is_browser_request(request: Request) -> bool:
    """Detect if the request is from a browser (vs API client).

    Checks the Accept header for text/html, which browsers send
    but API clients (curl, httpx, etc.) typically don't.
    """
    accept = request.headers.get("accept", "")
    return "text/html" in accept


class LoginRedirectMiddleware(BaseHTTPMiddleware):
    """Middleware that redirects unauthenticated browser requests to the login page.

    For API requests (Accept: application/json or no Accept header), the standard
    401 JSON response is returned. For browser requests (Accept: text/html) to
    protected paths without a valid Authorization header, a 302 redirect to the
    login page is returned with a Location header.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path

        # Skip public paths and WebSocket upgrades
        if _is_public_path(path) or request.headers.get("upgrade") == "websocket":
            return await call_next(request)

        # Check for missing auth on protected API paths
        if path.startswith("/api/"):
            auth_header = request.headers.get("authorization", "")
            has_auth = auth_header.startswith("Bearer ")

            if not has_auth:
                if _is_browser_request(request):
                    # Browser request: redirect to login page
                    redirect_url = f"{LOGIN_PAGE_URL}?redirect_url={path}"
                    return RedirectResponse(
                        url=redirect_url,
                        status_code=status.HTTP_302_FOUND,
                    )

        response = await call_next(request)

        # Add Location header to 401 responses on API paths so clients
        # know where to redirect for authentication
        if response.status_code == 401 and path.startswith("/api/"):
            login_url = f"{LOGIN_PAGE_URL}?redirect_url={path}"
            response.headers["Location"] = login_url

        return response
