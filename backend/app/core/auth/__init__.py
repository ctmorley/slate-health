"""Authentication and authorization — JWT tokens, RBAC middleware, SAML, OIDC."""

from app.core.auth.jwt import (
    InvalidTokenError,
    JWTError,
    TokenExpiredError,
    TokenPayload,
    create_access_token,
    create_refresh_token,
    verify_refresh_token,
    verify_token,
)
from app.core.auth.middleware import (
    CurrentUser,
    LoginRedirectMiddleware,
    RequireAdmin,
    RequireReviewer,
    RequireViewer,
    get_current_user,
    require_role,
)
from app.core.auth.oidc import OIDCError, OIDCProvider
from app.core.auth.saml import SAMLError, SAMLServiceProvider

__all__ = [
    "JWTError",
    "TokenExpiredError",
    "InvalidTokenError",
    "TokenPayload",
    "create_access_token",
    "create_refresh_token",
    "verify_token",
    "verify_refresh_token",
    "CurrentUser",
    "LoginRedirectMiddleware",
    "RequireAdmin",
    "RequireReviewer",
    "RequireViewer",
    "get_current_user",
    "require_role",
    "SAMLError",
    "SAMLServiceProvider",
    "OIDCError",
    "OIDCProvider",
]
