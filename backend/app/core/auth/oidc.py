"""OpenID Connect (OIDC) Relying Party implementation.

Handles authorization code flow: redirect to IdP, exchange code for tokens,
validate ID token (with signature verification via JWKS), and extract user
info claims. Integrates authlib for standards-compliant token handling.
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Try to import authlib for proper JWT/JWKS validation
try:
    from authlib.jose import JsonWebToken, JsonWebKey
    from authlib.jose.errors import (
        DecodeError as AuthlibDecodeError,
        ExpiredTokenError as AuthlibExpiredError,
        InvalidClaimError as AuthlibInvalidClaimError,
    )

    HAS_AUTHLIB = True
    logger.info("authlib loaded — OIDC ID token signature validation enabled")
except ImportError:
    HAS_AUTHLIB = False
    logger.warning(
        "authlib not available — OIDC ID tokens will be decoded without "
        "signature verification. Install authlib for production use."
    )


class OIDCError(Exception):
    """Raised for OIDC-related errors."""

    pass


# ── OIDC State Store ────────────────────────────────────────────────
# In-memory store kept as a fast-path cache; the authoritative store
# is the database (OIDCStateEntry model), which survives restarts and
# is shared across workers.

_oidc_state_store: dict[str, dict[str, Any]] = {}
_STATE_TTL_SECONDS = 600  # 10 minutes


def store_oidc_state(state: str, nonce: str) -> None:
    """Store OIDC state and nonce in the in-memory cache.

    For production use, also call ``db_store_oidc_state`` to persist to DB.
    """
    _cleanup_expired_states()
    _oidc_state_store[state] = {
        "nonce": nonce,
        "created_at": time.time(),
    }


def validate_and_consume_oidc_state(state: str) -> str | None:
    """Validate and consume an OIDC state from the in-memory cache.

    Returns the associated nonce if valid, None otherwise.
    The state is consumed (deleted) to prevent replay.

    For production use, prefer ``db_validate_and_consume_oidc_state``
    which checks the database (survives restarts/multi-worker).
    """
    _cleanup_expired_states()
    entry = _oidc_state_store.pop(state, None)
    if entry is None:
        return None
    if time.time() - entry["created_at"] > _STATE_TTL_SECONDS:
        return None
    return entry["nonce"]


def _cleanup_expired_states() -> None:
    """Remove expired state entries."""
    now = time.time()
    expired = [k for k, v in _oidc_state_store.items()
               if now - v["created_at"] > _STATE_TTL_SECONDS]
    for k in expired:
        _oidc_state_store.pop(k, None)


# ── Database-Backed State Store ─────────────────────────────────────


async def db_store_oidc_state(
    db: Any,  # AsyncSession — typed as Any to avoid circular import
    state: str,
    nonce: str,
) -> None:
    """Persist OIDC state+nonce to the database for cross-worker/restart safety.

    Also stores in the in-memory cache for fast local lookups.
    """
    from datetime import datetime as _dt, timezone as _tz
    from app.models.oidc_state import OIDCStateEntry
    import uuid as _uuid

    # In-memory cache (fast path for same-worker)
    store_oidc_state(state, nonce)

    # DB persistence (survives restarts, shared across workers)
    entry = OIDCStateEntry(
        id=_uuid.uuid4(),
        state=state,
        nonce=nonce,
        created_at=_dt.now(_tz.utc),
    )
    db.add(entry)
    await db.flush()
    logger.debug("OIDC state persisted to DB: state=%s...", state[:10])


async def db_validate_and_consume_oidc_state(
    db: Any,  # AsyncSession
    state: str,
) -> str | None:
    """Validate and consume OIDC state from DB (with in-memory fast path).

    Checks in-memory cache first, then falls back to database.
    The entry is deleted from both stores to prevent replay.

    Returns the nonce if valid, None otherwise.
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from sqlalchemy import select, delete
    from app.models.oidc_state import OIDCStateEntry

    # Fast path: check in-memory cache
    nonce = validate_and_consume_oidc_state(state)
    if nonce is not None:
        # Also remove from DB to stay consistent
        await db.execute(
            delete(OIDCStateEntry).where(OIDCStateEntry.state == state)
        )
        await db.flush()
        return nonce

    # Slow path: check database (state might have been stored by another worker)
    cutoff = _dt.now(_tz.utc) - _td(seconds=_STATE_TTL_SECONDS)
    stmt = select(OIDCStateEntry).where(
        OIDCStateEntry.state == state,
        OIDCStateEntry.created_at > cutoff,
    )
    result = await db.execute(stmt)
    entry = result.scalar_one_or_none()

    if entry is None:
        return None

    nonce = entry.nonce

    # Consume: delete from DB
    await db.execute(
        delete(OIDCStateEntry).where(OIDCStateEntry.state == state)
    )
    await db.flush()

    # Clean up old expired entries opportunistically
    await db.execute(
        delete(OIDCStateEntry).where(OIDCStateEntry.created_at <= cutoff)
    )

    logger.debug("OIDC state validated from DB: state=%s...", state[:10])
    return nonce


class OIDCProvider:
    """OpenID Connect Relying Party that handles the authorization code flow.

    Uses authlib for ID token signature verification when available,
    with fallback to unverified decoding for development/testing.
    """

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        discovery_url: str | None = None,
        redirect_uri: str | None = None,
        scopes: str | None = None,
    ) -> None:
        self.client_id = client_id or settings.oidc_client_id
        self.client_secret = client_secret or settings.oidc_client_secret
        self.discovery_url = discovery_url or settings.oidc_discovery_url
        self.redirect_uri = redirect_uri or settings.oidc_redirect_uri
        self.scopes = scopes or settings.oidc_scopes

        # Cached discovery document
        self._discovery_doc: dict[str, Any] | None = None
        # Cached JWKS keys
        self._jwks: dict[str, Any] | None = None

    async def discover(self) -> dict[str, Any]:
        """Fetch and cache the OIDC discovery document.

        Uses retry with backoff for transient network failures.

        Returns:
            OpenID Configuration document as a dict.
        """
        if self._discovery_doc is not None:
            return self._discovery_doc

        if not self.discovery_url:
            raise OIDCError("OIDC discovery URL is not configured")

        try:
            from app.core.resilience import resilient_http_get
            resp = await resilient_http_get(self.discovery_url, timeout=10.0)
            resp.raise_for_status()
            self._discovery_doc = resp.json()
            logger.info("OIDC discovery document fetched from %s", self.discovery_url)
            return self._discovery_doc
        except httpx.HTTPError as exc:
            raise OIDCError(f"Failed to fetch OIDC discovery document: {exc}")

    def set_discovery_doc(self, doc: dict[str, Any]) -> None:
        """Manually set the discovery document (for testing)."""
        self._discovery_doc = doc

    @property
    def has_discovery_doc(self) -> bool:
        """Return True if a discovery document has been loaded."""
        return self._discovery_doc is not None

    def get_authorization_endpoint(self) -> str | None:
        """Return the authorization endpoint from the discovery document, or None."""
        if self._discovery_doc:
            return self._discovery_doc.get("authorization_endpoint")
        return None

    async def _fetch_jwks(self) -> dict[str, Any] | None:
        """Fetch the JWKS from the IdP for token signature verification.

        Uses retry with backoff for transient network failures.
        """
        if self._jwks is not None:
            return self._jwks

        jwks_uri = None
        if self._discovery_doc:
            jwks_uri = self._discovery_doc.get("jwks_uri")

        if not jwks_uri:
            return None

        try:
            from app.core.resilience import resilient_http_get
            resp = await resilient_http_get(jwks_uri, timeout=10.0)
            resp.raise_for_status()
            self._jwks = resp.json()
            logger.info("OIDC JWKS fetched from %s", jwks_uri)
            return self._jwks
        except httpx.HTTPError as exc:
            logger.warning("Failed to fetch JWKS: %s", exc)
            return None

    def create_authorization_url(
        self,
        state: str | None = None,
        nonce: str | None = None,
        authorization_endpoint: str | None = None,
        *,
        persist_state: bool = True,
    ) -> dict[str, str]:
        """Build the OIDC authorization redirect URL.

        Generates state/nonce for CSRF and replay protection.
        By default stores state in the in-memory cache. For production,
        callers should also persist via ``db_store_oidc_state``.

        Args:
            state: CSRF protection state parameter. Generated if not provided.
            nonce: ID token replay protection nonce. Generated if not provided.
            authorization_endpoint: Override authorization endpoint URL.
            persist_state: If True (default), store state in in-memory cache.
                Callers should additionally call ``db_store_oidc_state`` for
                cross-worker/restart durability.

        Returns:
            Dict with 'url', 'state', 'nonce'.
        """
        if not self.client_id:
            raise OIDCError("OIDC client_id is not configured")

        auth_endpoint = authorization_endpoint
        if not auth_endpoint:
            if self._discovery_doc:
                auth_endpoint = self._discovery_doc.get("authorization_endpoint")
            if not auth_endpoint:
                raise OIDCError(
                    "Authorization endpoint not available. Call discover() first or provide it directly."
                )

        state = state or secrets.token_urlsafe(32)
        nonce = nonce or secrets.token_urlsafe(32)

        # Store state and nonce in in-memory cache for callback validation
        if persist_state:
            store_oidc_state(state, nonce)

        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.scopes,
            "state": state,
            "nonce": nonce,
        }

        url = f"{auth_endpoint}?{urlencode(params)}"
        logger.info("OIDC authorization URL created: endpoint=%s", auth_endpoint)

        return {
            "url": url,
            "state": state,
            "nonce": nonce,
        }

    async def ensure_discovered(self) -> dict[str, Any] | None:
        """Ensure the discovery document is loaded, re-fetching if needed.

        Unlike ``discover()`` which only fetches once (caches), this method
        will re-fetch if the cache is empty — making callbacks resilient
        to process restarts and multi-worker deployments.

        Returns:
            Discovery document dict, or None if discovery is not configured.
        """
        if self._discovery_doc is not None:
            return self._discovery_doc
        if self.discovery_url:
            return await self.discover()
        return None

    async def exchange_code(
        self,
        code: str,
        token_endpoint: str | None = None,
    ) -> dict[str, Any]:
        """Exchange authorization code for tokens.

        Automatically re-discovers the token endpoint if the discovery
        cache is empty (e.g. after a process restart or on a different worker).

        Args:
            code: Authorization code from callback.
            token_endpoint: Override token endpoint URL.

        Returns:
            Token response dict with 'access_token', 'id_token', etc.
        """
        endpoint = token_endpoint
        if not endpoint:
            # Auto-discover if cache is empty (resilience across restarts/workers)
            if not self._discovery_doc and self.discovery_url:
                try:
                    await self.ensure_discovered()
                except OIDCError as exc:
                    logger.warning("Auto-discovery failed during code exchange: %s", exc)
            if self._discovery_doc:
                endpoint = self._discovery_doc.get("token_endpoint")
            if not endpoint:
                raise OIDCError("Token endpoint not available. Call discover() first or provide it.")

        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

        try:
            from app.core.resilience import resilient_http_post
            resp = await resilient_http_post(endpoint, data=data, timeout=10.0)
            resp.raise_for_status()
            token_data = resp.json()
            logger.info("OIDC token exchange successful")
            return token_data
        except httpx.HTTPStatusError as exc:
            error_body = exc.response.text if exc.response else "no body"
            raise OIDCError(f"Token exchange failed ({exc.response.status_code}): {error_body}")
        except httpx.HTTPError as exc:
            raise OIDCError(f"Token exchange request failed: {exc}")

    async def get_userinfo(
        self,
        access_token: str,
        userinfo_endpoint: str | None = None,
    ) -> dict[str, Any]:
        """Fetch user info from the OIDC userinfo endpoint.

        Args:
            access_token: OAuth2 access token.
            userinfo_endpoint: Override userinfo endpoint URL.

        Returns:
            User info claims dict.
        """
        endpoint = userinfo_endpoint
        if not endpoint:
            # Auto-discover if cache is empty
            if not self._discovery_doc and self.discovery_url:
                try:
                    await self.ensure_discovered()
                except OIDCError:
                    pass
            if self._discovery_doc:
                endpoint = self._discovery_doc.get("userinfo_endpoint")
            if not endpoint:
                raise OIDCError("Userinfo endpoint not available.")

        try:
            from app.core.resilience import resilient_http_get
            resp = await resilient_http_get(
                endpoint,
                timeout=10.0,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            userinfo = resp.json()
            logger.info("OIDC userinfo fetched: sub=%s", userinfo.get("sub"))
            return userinfo
        except httpx.HTTPError as exc:
            raise OIDCError(f"Failed to fetch userinfo: {exc}")

    async def verify_id_token(
        self,
        id_token: str,
        nonce: str | None = None,
    ) -> dict[str, Any]:
        """Verify an ID token with signature validation via JWKS.

        Uses authlib to validate the JWT signature against the IdP's JWKS,
        and checks issuer, audience, expiry, and nonce claims.

        Falls back to unverified decoding if authlib is unavailable or
        JWKS cannot be fetched.

        Args:
            id_token: JWT ID token string.
            nonce: Expected nonce value for replay protection.

        Returns:
            Decoded claims dict.
        """
        if HAS_AUTHLIB:
            jwks = await self._fetch_jwks()
            if jwks:
                try:
                    return self._verify_with_authlib(id_token, jwks, nonce)
                except OIDCError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "authlib ID token verification failed: %s", exc
                    )
                    # In production, do not fall back to unverified decoding
                    if not settings.debug:
                        raise OIDCError(
                            f"ID token signature verification failed and fallback is disabled "
                            f"in production mode: {exc}"
                        )

        # In production mode (debug=False), refuse to decode without verification
        if not settings.debug:
            if not HAS_AUTHLIB:
                raise OIDCError(
                    "ID token signature validation unavailable: authlib is not installed. "
                    "Install authlib for production use, or set SLATE_DEBUG=true for development."
                )
            raise OIDCError(
                "ID token signature validation unavailable: JWKS could not be fetched. "
                "Ensure the IdP's jwks_uri is reachable, or set SLATE_DEBUG=true for development."
            )

        # Development/debug mode only: allow fallback without signature validation
        logger.warning(
            "DEBUG MODE: ID token decoded without signature verification. "
            "This is NOT suitable for production."
        )
        claims = self.parse_id_token_unverified(id_token)

        # Still validate basic claims even without signature
        self._validate_claims(claims, nonce)

        return claims

    def _verify_with_authlib(
        self,
        id_token: str,
        jwks: dict[str, Any],
        nonce: str | None = None,
    ) -> dict[str, Any]:
        """Verify ID token using authlib JWKS validation."""
        jwt = JsonWebToken(["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"])

        key_set = JsonWebKey.import_key_set(jwks)

        claims_options = {
            "iss": {"essential": True},
            "sub": {"essential": True},
            "aud": {"essential": True},
            "exp": {"essential": True},
            "iat": {"essential": True},
        }

        try:
            claims = jwt.decode(id_token, key_set, claims_options=claims_options)
        except AuthlibExpiredError:
            raise OIDCError("ID token has expired")
        except (AuthlibDecodeError, AuthlibInvalidClaimError) as exc:
            raise OIDCError(f"ID token validation failed: {exc}")
        except Exception as exc:
            raise OIDCError(f"ID token verification error: {exc}")

        # Validate audience
        aud = claims.get("aud")
        if isinstance(aud, list):
            if self.client_id not in aud:
                raise OIDCError(f"ID token audience mismatch: {aud}")
        elif aud != self.client_id:
            raise OIDCError(f"ID token audience mismatch: expected '{self.client_id}', got '{aud}'")

        # Validate issuer against discovery doc
        if self._discovery_doc:
            expected_issuer = self._discovery_doc.get("issuer")
            if expected_issuer and claims.get("iss") != expected_issuer:
                raise OIDCError(
                    f"ID token issuer mismatch: expected '{expected_issuer}', "
                    f"got '{claims.get('iss')}'"
                )

        # Validate nonce
        if nonce and claims.get("nonce") != nonce:
            raise OIDCError("ID token nonce mismatch — possible replay attack")

        logger.info("ID token verified via authlib: sub=%s, iss=%s", claims.get("sub"), claims.get("iss"))
        return dict(claims)

    def _validate_claims(self, claims: dict[str, Any], nonce: str | None = None) -> None:
        """Validate basic claims even without signature verification."""
        # Check expiry
        exp = claims.get("exp")
        if exp and isinstance(exp, (int, float)):
            if time.time() > exp:
                raise OIDCError("ID token has expired")

        # Validate audience
        aud = claims.get("aud")
        if aud and self.client_id:
            if isinstance(aud, list):
                if self.client_id not in aud:
                    raise OIDCError(f"ID token audience mismatch: {aud}")
            elif aud != self.client_id:
                raise OIDCError(f"ID token audience mismatch: expected '{self.client_id}', got '{aud}'")

        # Validate issuer
        if self._discovery_doc:
            expected_issuer = self._discovery_doc.get("issuer")
            if expected_issuer and claims.get("iss") != expected_issuer:
                raise OIDCError(
                    f"ID token issuer mismatch: expected '{expected_issuer}', "
                    f"got '{claims.get('iss')}'"
                )

        # Validate nonce
        if nonce and claims.get("nonce") != nonce:
            raise OIDCError("ID token nonce mismatch — possible replay attack")

    def parse_id_token_unverified(self, id_token: str) -> dict[str, Any]:
        """Decode an ID token WITHOUT signature verification.

        This method is provided as a fallback for environments where JWKS
        is unavailable. In production, use verify_id_token() instead.

        Args:
            id_token: JWT ID token string.

        Returns:
            Decoded claims dict.
        """
        import json
        import base64

        try:
            parts = id_token.split(".")
            if len(parts) != 3:
                raise OIDCError("ID token does not have 3 parts")

            # Decode payload (part 1)
            payload_b64 = parts[1]
            # Add padding if needed
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += "=" * padding

            payload_bytes = base64.urlsafe_b64decode(payload_b64)
            claims = json.loads(payload_bytes)
            return claims
        except (ValueError, json.JSONDecodeError) as exc:
            raise OIDCError(f"Failed to decode ID token: {exc}")
