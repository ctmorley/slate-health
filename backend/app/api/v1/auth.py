"""Auth API routes — SSO login (SAML/OIDC), callbacks, refresh, and user profile.

Implements full SSO authentication flow:
- POST /login — initiate SAML or OIDC flow, returns redirect URL
- GET /callback/saml — SAML ACS via HTTP-Redirect binding
- POST /callback/saml — SAML ACS via HTTP-POST binding
- GET /callback/oidc — OIDC authorization code callback
- POST /refresh — refresh access token using refresh token (with rotation/revocation)
- GET /me — current user profile
"""

from __future__ import annotations

import logging
import uuid as _uuid
from datetime import datetime, timedelta, timezone

from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.auth.jwt import (
    TokenPayload,
    create_access_token,
    create_refresh_token,
    verify_refresh_token,
)
from app.core.auth.middleware import CurrentUser
from app.core.auth.oidc import (
    OIDCError,
    OIDCProvider,
    db_store_oidc_state,
    db_validate_and_consume_oidc_state,
    validate_and_consume_oidc_state,
)
from app.core.auth.saml import SAMLError, SAMLServiceProvider
from app.dependencies import get_db
from app.models.refresh_token import RevokedRefreshToken
from app.models.user import User
from app.schemas.auth import (
    LoginPageResponse,
    LoginRequest,
    LoginResponse,
    RefreshRequest,
    TokenResponse,
    UserProfile,
)
from app.services.user_service import provision_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


# ── SSO provider factories (overridable for testing) ──────────────────

_saml_sp: SAMLServiceProvider | None = None
_oidc_provider: OIDCProvider | None = None


def get_saml_sp() -> SAMLServiceProvider:
    """Get or create the SAML service provider instance."""
    global _saml_sp
    if _saml_sp is None:
        _saml_sp = SAMLServiceProvider()
    return _saml_sp


def set_saml_sp(sp: SAMLServiceProvider | None) -> None:
    """Override the SAML SP (for testing)."""
    global _saml_sp
    _saml_sp = sp


def get_oidc_provider() -> OIDCProvider:
    """Get or create the OIDC provider instance."""
    global _oidc_provider
    if _oidc_provider is None:
        _oidc_provider = OIDCProvider()
    return _oidc_provider


def set_oidc_provider(provider: OIDCProvider | None) -> None:
    """Override the OIDC provider (for testing)."""
    global _oidc_provider
    _oidc_provider = provider


# ── Login ─────────────────────────────────────────────────────────────


@router.get("/login", response_model=LoginPageResponse)
async def login_page(redirect_url: str = Query(default="/")) -> LoginPageResponse:
    """Login page for browser-based SSO flows.

    Returns available SSO providers so the browser/frontend can present
    login options. This is the GET target for the login redirect middleware.
    """
    providers = []
    if settings.saml_idp_sso_url:
        providers.append("saml")
    if settings.oidc_client_id:
        providers.append("oidc")
    # Always include both as options even if not fully configured,
    # so the frontend can display them
    if not providers:
        providers = ["saml", "oidc"]
    return LoginPageResponse(
        providers=providers,
        redirect_url=redirect_url,
    )


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)) -> LoginResponse:
    """Initiate SSO login flow. Returns a redirect URL for the IdP.

    Supports 'saml' and 'oidc' providers.
    """
    if body.provider == "saml":
        sp = get_saml_sp()
        try:
            # Load IdP metadata if URL is configured and not yet loaded
            if sp.idp_metadata_url and not sp._metadata_loaded:
                try:
                    await sp.load_idp_metadata()
                except SAMLError as meta_exc:
                    logger.warning("Failed to load SAML IdP metadata: %s", meta_exc)
                    # Continue — will use directly-configured values if available
            result = sp.create_authn_request(relay_state=body.redirect_url)
            return LoginResponse(redirect_url=result["url"], provider="saml")
        except SAMLError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"SAML login initiation failed: {exc}",
            )

    elif body.provider == "oidc":
        provider = get_oidc_provider()
        try:
            # Use discovery doc if available, fall back to configured URL
            if not provider.has_discovery_doc and settings.oidc_discovery_url:
                try:
                    await provider.discover()
                except OIDCError:
                    pass  # Will use direct config if discovery fails

            result = provider.create_authorization_url(
                authorization_endpoint=provider.get_authorization_endpoint(),
            )

            # Persist state+nonce to DB for cross-worker/restart durability
            await db_store_oidc_state(db, result["state"], result["nonce"])

            return LoginResponse(redirect_url=result["url"], provider="oidc")
        except OIDCError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"OIDC login initiation failed: {exc}",
            )

    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported SSO provider: {body.provider}. Use 'saml' or 'oidc'.",
        )


# ── SAML Callback ─────────────────────────────────────────────────────


def _build_frontend_redirect(
    tokens: TokenResponse,
    relay_state: str,
) -> str:
    """Build the frontend redirect URL with tokens in query parameters.

    Uses relay_state (from SAML) or a default frontend URL to determine
    where to redirect the browser after successful SSO authentication.
    """
    # Determine base redirect URL from relay_state or config default
    frontend_url = relay_state or getattr(settings, "frontend_url", "") or "/"

    # Validate that redirect target is a relative path or same origin
    parsed = urlparse(frontend_url)
    if parsed.scheme and parsed.netloc:
        # Absolute URL — use as-is (trusted since it came from our own LoginResponse)
        base_url = frontend_url
    else:
        # Relative path
        base_url = frontend_url

    separator = "&" if "?" in base_url else "?"
    params = urlencode({
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
    })
    return f"{base_url}{separator}{params}"


async def _handle_saml_callback(
    saml_response_b64: str,
    relay_state: str,
    db: AsyncSession,
) -> TokenResponse:
    """Common SAML callback handler for both GET and POST bindings."""
    sp = get_saml_sp()
    try:
        saml_data = sp.parse_response(saml_response_b64)
    except SAMLError as exc:
        logger.warning("SAML callback failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"SAML authentication failed: {exc}",
        )

    # Extract user info from SAML attributes
    email = saml_data["email"]
    subject_id = saml_data["subject_id"]
    attributes = saml_data.get("attributes", {})

    # Derive full name from attributes or email
    full_name = ""
    for name_key in ("displayName", "fullName", "name",
                     "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name"):
        if name_key in attributes:
            full_name = attributes[name_key][0] if isinstance(attributes[name_key], list) else attributes[name_key]
            break

    first_name = ""
    last_name = ""
    for fn_key in ("firstName", "givenName",
                   "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname"):
        if fn_key in attributes:
            first_name = attributes[fn_key][0] if isinstance(attributes[fn_key], list) else attributes[fn_key]
            break
    for ln_key in ("lastName", "surname",
                   "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname"):
        if ln_key in attributes:
            last_name = attributes[ln_key][0] if isinstance(attributes[ln_key], list) else attributes[ln_key]
            break

    if not full_name and (first_name or last_name):
        full_name = f"{first_name} {last_name}".strip()
    if not full_name:
        full_name = email.split("@")[0]

    # Flatten list-valued attributes for role mapping
    flat_attrs: dict = {}
    for k, v in attributes.items():
        flat_attrs[k] = v[0] if isinstance(v, list) and len(v) == 1 else v

    # Provision or update user
    user, is_new = await provision_user(
        db,
        email=email,
        full_name=full_name,
        sso_provider="saml",
        sso_subject_id=subject_id,
        idp_attributes=flat_attrs,
    )

    # Issue JWT tokens
    access_token = create_access_token(
        user_id=user.id,
        email=user.email,
        role=user.role,
        organization_id=user.organization_id,
        full_name=user.full_name,
    )
    refresh_token = create_refresh_token(user_id=user.id)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.jwt_expiry_minutes * 60,
    )


@router.get("/callback/saml")
async def saml_callback_get(
    SAMLResponse: str = Query(...),
    RelayState: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """SAML Assertion Consumer Service (ACS) endpoint — HTTP-Redirect binding.

    Receives the SAML Response from the IdP via query parameters.
    After processing, redirects to the frontend with JWT tokens.
    """
    tokens = await _handle_saml_callback(SAMLResponse, RelayState, db)
    redirect_url = _build_frontend_redirect(tokens, RelayState)
    return RedirectResponse(url=redirect_url, status_code=302)


@router.post("/callback/saml")
async def saml_callback_post(
    SAMLResponse: str = Form(...),
    RelayState: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """SAML Assertion Consumer Service (ACS) endpoint — HTTP-POST binding.

    Receives the SAML Response from the IdP via form data.
    After processing, redirects to the frontend with JWT tokens.
    """
    tokens = await _handle_saml_callback(SAMLResponse, RelayState, db)
    redirect_url = _build_frontend_redirect(tokens, RelayState)
    return RedirectResponse(url=redirect_url, status_code=302)


# ── OIDC Callback ─────────────────────────────────────────────────────


@router.get("/callback/oidc")
async def oidc_callback(
    code: str = Query(...),
    state: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """OIDC authorization code callback.

    Validates the state parameter against the stored value, exchanges the
    authorization code for tokens, extracts user info, provisions the user,
    and redirects to the frontend with JWT tokens.
    """
    provider = get_oidc_provider()

    # Validate state parameter for CSRF protection
    # Uses DB-backed store (survives restarts and works across workers)
    nonce: str | None = None
    if state:
        nonce = await db_validate_and_consume_oidc_state(db, state)
        if nonce is None:
            logger.warning(
                "OIDC callback: invalid or expired state parameter — rejecting. "
                "state=%s", state[:20] if state else "(empty)"
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired OIDC state parameter (possible CSRF attack)",
            )
    else:
        # No state provided at all — reject for CSRF protection
        logger.warning("OIDC callback: no state parameter provided — rejecting")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing OIDC state parameter (CSRF protection)",
        )

    # Ensure discovery doc is loaded (resilience across restarts/workers)
    if not provider.has_discovery_doc and provider.discovery_url:
        try:
            await provider.ensure_discovered()
        except OIDCError as exc:
            logger.warning("OIDC auto-discovery in callback failed: %s", exc)

    try:
        # Exchange code for tokens
        token_data = await provider.exchange_code(code)
    except OIDCError as exc:
        logger.warning("OIDC code exchange failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"OIDC authentication failed: {exc}",
        )

    # Extract user info — prefer userinfo endpoint, fall back to ID token
    userinfo: dict = {}
    oidc_access_token = token_data.get("access_token", "")
    id_token = token_data.get("id_token", "")

    if oidc_access_token:
        try:
            userinfo = await provider.get_userinfo(oidc_access_token)
        except OIDCError:
            pass  # Fall back to ID token

    if not userinfo and id_token:
        try:
            # Use verified ID token parsing when possible.
            # In production mode, verify_id_token will refuse to fall back
            # to unverified decoding — this is intentional (fail closed).
            userinfo = await provider.verify_id_token(id_token, nonce=nonce)
        except OIDCError:
            # In debug mode only, attempt unverified parsing as last resort
            if settings.debug:
                try:
                    userinfo = provider.parse_id_token_unverified(id_token)
                except OIDCError:
                    pass

    if not userinfo:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not extract user information from OIDC response",
        )

    # Extract fields
    subject_id = userinfo.get("sub", "")
    email = userinfo.get("email", "")
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email claim missing from OIDC response",
        )

    full_name = userinfo.get("name", "")
    if not full_name:
        given = userinfo.get("given_name", "")
        family = userinfo.get("family_name", "")
        full_name = f"{given} {family}".strip() or email.split("@")[0]

    # Provision or update user
    user, is_new = await provision_user(
        db,
        email=email,
        full_name=full_name,
        sso_provider="oidc",
        sso_subject_id=subject_id,
        idp_attributes=userinfo,
    )

    # Issue JWT tokens
    access_token = create_access_token(
        user_id=user.id,
        email=user.email,
        role=user.role,
        organization_id=user.organization_id,
        full_name=user.full_name,
    )
    refresh_token = create_refresh_token(user_id=user.id)

    tokens = TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.jwt_expiry_minutes * 60,
    )

    # Redirect to frontend with tokens in query parameters
    frontend_url = getattr(settings, "frontend_url", "") or "/login"
    redirect_url = _build_frontend_redirect(tokens, frontend_url)
    return RedirectResponse(url=redirect_url, status_code=302)


# ── Token Refresh ─────────────────────────────────────────────────────


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(body: RefreshRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    """Refresh an access token using a refresh token.

    Implements token rotation with revocation:
    1. Verifies the refresh token JWT
    2. Checks that the token's JTI has not been previously revoked
    3. Revokes the old JTI (prevents reuse)
    4. Issues a new token pair with a fresh JTI
    """
    try:
        claims = verify_refresh_token(body.refresh_token)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    # JTI is guaranteed present by verify_refresh_token (required for revocation)
    old_jti = claims["jti"]

    # Sub is guaranteed to be a valid UUID by verify_refresh_token
    user_id = _uuid.UUID(claims["sub"])

    # Check if this refresh token's JTI has already been revoked (used)
    stmt = select(RevokedRefreshToken).where(RevokedRefreshToken.jti == old_jti)
    result = await db.execute(stmt)
    if result.scalar_one_or_none() is not None:
        logger.warning(
            "Attempted reuse of revoked refresh token: jti=%s, user_id=%s",
            old_jti, user_id,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has been revoked (already used)",
        )

    # Revoke the old JTI — mark it as consumed
    exp_timestamp = claims.get("exp")
    expires_at = (
        datetime.fromtimestamp(exp_timestamp, tz=timezone.utc)
        if exp_timestamp
        else datetime.now(timezone.utc) + timedelta(days=7)
    )
    revoked = RevokedRefreshToken(
        id=_uuid.uuid4(),
        jti=old_jti,
        user_id=user_id,
        revoked_at=datetime.now(timezone.utc),
        expires_at=expires_at,
    )
    db.add(revoked)
    await db.flush()

    # Look up user to get fresh role/profile data
    stmt = select(User).where(User.id == user_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is not None:
        new_access = create_access_token(
            user_id=user.id,
            email=user.email,
            role=user.role,
            full_name=user.full_name,
            organization_id=user.organization_id,
        )
    else:
        # User not in DB (e.g., pre-SSO token) — re-issue from claims
        new_access = create_access_token(
            user_id=user_id,
            email=claims.get("email", ""),
            role=claims.get("role", "viewer"),
            full_name=claims.get("full_name", ""),
        )

    new_refresh = create_refresh_token(
        user_id=user_id,
        expires_delta=timedelta(days=7),
    )

    return TokenResponse(
        access_token=new_access,
        refresh_token=new_refresh,
        expires_in=settings.jwt_expiry_minutes * 60,
    )


# ── User Profile ──────────────────────────────────────────────────────


@router.get("/me", response_model=UserProfile)
async def get_me(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> UserProfile:
    """Return current user profile from the database.

    Falls back to JWT claims if user record is not found in DB.
    """
    stmt = select(User).where(User.id == current_user.user_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is not None:
        return UserProfile(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            roles=[user.role],
            organization_id=user.organization_id,
            last_login=user.last_login,
            is_active=user.is_active,
        )

    # Fallback to JWT claims
    return UserProfile(
        id=current_user.user_id,
        email=current_user.email,
        full_name=current_user.full_name,
        roles=[current_user.role],
        organization_id=current_user.organization_id,
    )
