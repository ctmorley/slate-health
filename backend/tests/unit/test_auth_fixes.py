"""Tests for Sprint 6 Iteration 2 & 3 fixes.

Covers:
- Refresh token revocation (old token rejected after use)
- SAML GET callback route
- Login redirect middleware for browser requests
- WebSocket authentication enforcement
- OIDC state validation
- python3-saml and authlib integration
- GET /api/v1/auth/login handler (Iteration 3)
- Refresh endpoint malformed sub handling (Iteration 3)
- SAML IdP metadata URL ingestion (Iteration 3)
- Fail-closed security for SAML/OIDC in production mode (Iteration 3)
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

import jwt as pyjwt

from app.config import settings
from app.core.auth.jwt import (
    InvalidTokenError,
    create_access_token,
    create_refresh_token,
    verify_refresh_token,
    verify_token,
)
from app.core.auth.oidc import (
    OIDCProvider,
    _oidc_state_store,
    store_oidc_state,
    validate_and_consume_oidc_state,
)
from app.core.auth.saml import SAMLError, SAMLServiceProvider


# ── Refresh Token Revocation Tests ────────────────────────────────────


class TestRefreshTokenRevocation:
    """Verify that refresh tokens are single-use (revoked after consumption)."""

    @pytest.mark.asyncio
    async def test_refresh_token_cannot_be_reused(self, client: AsyncClient):
        """Using the same refresh token twice: first call 200, second call 401."""
        user_id = uuid.uuid4()
        refresh = create_refresh_token(user_id=user_id)

        # First use — should succeed
        resp1 = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh},
        )
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert "access_token" in data1
        assert "refresh_token" in data1

        # Second use of the SAME token — should be rejected
        resp2 = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh},
        )
        assert resp2.status_code == 401
        assert "revoked" in resp2.json()["detail"].lower() or "already used" in resp2.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_new_refresh_token_from_rotation_works(self, client: AsyncClient):
        """The new refresh token issued during rotation is valid for the next refresh."""
        user_id = uuid.uuid4()
        refresh = create_refresh_token(user_id=user_id)

        # First refresh — get new token pair
        resp1 = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh},
        )
        assert resp1.status_code == 200
        new_refresh = resp1.json()["refresh_token"]

        # Use the new refresh token — should succeed
        resp2 = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": new_refresh},
        )
        assert resp2.status_code == 200

    @pytest.mark.asyncio
    async def test_old_refresh_token_rejected_after_rotation(self, client: AsyncClient):
        """After rotating to a new refresh token, the old one is permanently rejected."""
        user_id = uuid.uuid4()
        old_refresh = create_refresh_token(user_id=user_id)

        # Rotate
        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": old_refresh},
        )
        assert resp.status_code == 200

        # Try old token again — must fail
        resp2 = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": old_refresh},
        )
        assert resp2.status_code == 401


# ── SAML GET Callback Tests ───────────────────────────────────────────


class TestSAMLGetCallback:
    """Verify that GET /api/v1/auth/callback/saml works (not just POST)."""

    @pytest.mark.asyncio
    async def test_saml_get_callback_returns_200(self, client: AsyncClient):
        """GET /callback/saml with SAMLResponse query param works."""
        from app.api.v1.auth import set_saml_sp

        mock_sp = MagicMock(spec=SAMLServiceProvider)
        mock_sp.parse_response.return_value = {
            "subject_id": "get-saml-user-123",
            "email": "get-saml@example.com",
            "attributes": {
                "displayName": ["GET SAML User"],
                "role": ["reviewer"],
            },
            "session_index": "session-get",
        }
        set_saml_sp(mock_sp)

        try:
            resp = await client.get(
                "/api/v1/auth/callback/saml",
                params={
                    "SAMLResponse": "mock-saml-response-b64",
                    "RelayState": "/dashboard",
                },
                follow_redirects=False,
            )
            # Callbacks redirect to frontend with tokens in query params
            assert resp.status_code == 302
            from urllib.parse import urlparse, parse_qs
            params = parse_qs(urlparse(resp.headers["location"]).query)
            assert "access_token" in params
            assert "refresh_token" in params

            # Verify the JWT contains correct claims
            payload = verify_token(params["access_token"][0])
            assert payload.email == "get-saml@example.com"
            assert payload.role == "reviewer"
        finally:
            set_saml_sp(None)

    @pytest.mark.asyncio
    async def test_saml_get_callback_does_not_return_405(self, client: AsyncClient):
        """GET /callback/saml should not return 405 Method Not Allowed."""
        from app.api.v1.auth import set_saml_sp

        mock_sp = MagicMock(spec=SAMLServiceProvider)
        mock_sp.parse_response.return_value = {
            "subject_id": "test",
            "email": "test@example.com",
            "attributes": {},
            "session_index": "",
        }
        set_saml_sp(mock_sp)

        try:
            resp = await client.get(
                "/api/v1/auth/callback/saml",
                params={"SAMLResponse": "test"},
            )
            assert resp.status_code != 405
        finally:
            set_saml_sp(None)

    @pytest.mark.asyncio
    async def test_saml_post_callback_still_works(self, client: AsyncClient):
        """POST /callback/saml continues to work alongside GET."""
        from app.api.v1.auth import set_saml_sp

        mock_sp = MagicMock(spec=SAMLServiceProvider)
        mock_sp.parse_response.return_value = {
            "subject_id": "post-saml-user",
            "email": "post-saml@example.com",
            "attributes": {},
            "session_index": "",
        }
        set_saml_sp(mock_sp)

        try:
            resp = await client.post(
                "/api/v1/auth/callback/saml",
                data={"SAMLResponse": "mock-response"},
                follow_redirects=False,
            )
            assert resp.status_code == 302
        finally:
            set_saml_sp(None)


# ── Login Redirect Middleware Tests ───────────────────────────────────


class TestLoginRedirect:
    """Verify that unauthenticated browser requests are redirected to login."""

    @pytest.mark.asyncio
    async def test_browser_request_redirected_to_login(self, client: AsyncClient):
        """Browser requests to protected routes get 302 redirect with Location header."""
        resp = await client.get(
            "/api/v1/dashboard/summary",
            headers={"Accept": "text/html,application/xhtml+xml"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "Location" in resp.headers
        location = resp.headers["Location"]
        assert "/auth/login" in location

    @pytest.mark.asyncio
    async def test_api_request_returns_401_not_redirect(self, client: AsyncClient):
        """API requests (no text/html Accept) still get 401 JSON response."""
        resp = await client.get(
            "/api/v1/dashboard/summary",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_browser_with_token_not_redirected(self, client: AsyncClient):
        """Browser requests WITH valid token are not redirected."""
        token = create_access_token(
            user_id=uuid.uuid4(),
            email="browser@test.com",
            role="viewer",
            full_name="Browser User",
        )
        resp = await client.get(
            "/api/v1/dashboard/summary",
            headers={
                "Accept": "text/html",
                "Authorization": f"Bearer {token}",
            },
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_public_endpoints_not_redirected(self, client: AsyncClient):
        """Public endpoints are not affected by redirect middleware."""
        resp = await client.get(
            "/health",
            headers={"Accept": "text/html"},
        )
        assert resp.status_code == 200


# ── OIDC State Validation Tests ───────────────────────────────────────


class TestOIDCStateValidation:
    """Verify OIDC state parameter is stored and validated."""

    def test_store_and_validate_state(self):
        """State is stored and can be validated/consumed."""
        store_oidc_state("test-state-123", "test-nonce-456")
        nonce = validate_and_consume_oidc_state("test-state-123")
        assert nonce == "test-nonce-456"

    def test_state_consumed_on_validation(self):
        """State is consumed (deleted) after validation — single use."""
        store_oidc_state("single-use-state", "nonce")
        assert validate_and_consume_oidc_state("single-use-state") == "nonce"
        # Second call should return None (consumed)
        assert validate_and_consume_oidc_state("single-use-state") is None

    def test_invalid_state_returns_none(self):
        """Unknown state returns None."""
        result = validate_and_consume_oidc_state("nonexistent-state")
        assert result is None

    def test_authorization_url_stores_state(self):
        """create_authorization_url stores state for later validation."""
        provider = OIDCProvider(client_id="test-client")
        provider.set_discovery_doc({
            "authorization_endpoint": "https://idp.example.com/auth",
        })

        result = provider.create_authorization_url()
        state = result["state"]
        nonce = result["nonce"]

        # State should be retrievable
        stored_nonce = validate_and_consume_oidc_state(state)
        assert stored_nonce == nonce


# ── python3-saml / authlib Integration Tests ──────────────────────────


class TestLibraryIntegration:
    """Verify that python3-saml and authlib are importable and used."""

    def test_python3_saml_importable(self):
        """python3-saml library is installed and importable."""
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
        from onelogin.saml2.settings import OneLogin_Saml2_Settings
        assert OneLogin_Saml2_Auth is not None
        assert OneLogin_Saml2_Settings is not None

    def test_authlib_importable(self):
        """authlib library is installed and importable."""
        from authlib.jose import JsonWebToken, JsonWebKey
        assert JsonWebToken is not None
        assert JsonWebKey is not None

    def test_saml_module_detects_python3_saml(self):
        """SAML module correctly detects python3-saml availability."""
        from app.core.auth.saml import HAS_PYTHON3_SAML
        assert HAS_PYTHON3_SAML is True

    def test_oidc_module_detects_authlib(self):
        """OIDC module correctly detects authlib availability."""
        from app.core.auth.oidc import HAS_AUTHLIB
        assert HAS_AUTHLIB is True

    def test_saml_sp_has_python3_saml_settings_method(self):
        """SAMLServiceProvider has method for generating python3-saml settings."""
        sp = SAMLServiceProvider(
            sp_entity_id="https://sp.test",
            sp_acs_url="https://sp.test/acs",
            idp_entity_id="https://idp.test",
            idp_sso_url="https://idp.test/sso",
            idp_x509_cert="MIIBxTCCAW...",
        )
        saml_settings = sp._get_saml_settings()
        assert saml_settings["sp"]["entityId"] == "https://sp.test"
        assert saml_settings["idp"]["entityId"] == "https://idp.test"
        assert saml_settings["idp"]["x509cert"] == "MIIBxTCCAW..."

    def test_oidc_provider_has_verify_id_token(self):
        """OIDCProvider has verify_id_token method for authlib-based validation."""
        provider = OIDCProvider(client_id="test")
        assert hasattr(provider, "verify_id_token")
        assert callable(provider.verify_id_token)


# ── SAML Security Validation Tests ────────────────────────────────────


class TestSAMLValidation:
    """Verify SAML response validation improvements."""

    def test_fallback_validates_conditions_time_bounds(self):
        """Fallback parser validates Conditions NotOnOrAfter (expired assertion rejected)."""
        import base64
        sp = SAMLServiceProvider()

        # Build a SAML response with expired conditions
        xml = (
            '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
            'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">'
            '<samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>'
            '<saml:Assertion>'
            '<saml:Conditions NotOnOrAfter="2020-01-01T00:00:00Z"/>'
            '<saml:Subject><saml:NameID>user@test.com</saml:NameID></saml:Subject>'
            '</saml:Assertion>'
            '</samlp:Response>'
        )
        b64 = base64.b64encode(xml.encode()).decode()

        # Use debug mode to allow fallback parser (testing the parser, not production gate)
        with patch("app.core.auth.saml.settings") as mock_settings:
            mock_settings.debug = True
            with pytest.raises(SAMLError, match="expired"):
                sp.parse_response(b64)

    def test_fallback_validates_audience_restriction(self):
        """Fallback parser validates AudienceRestriction against SP entity ID."""
        import base64
        sp = SAMLServiceProvider(sp_entity_id="https://correct-sp.example.com")

        xml = (
            '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
            'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">'
            '<samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>'
            '<saml:Assertion>'
            '<saml:Conditions>'
            '<saml:AudienceRestriction><saml:Audience>https://wrong-sp.example.com</saml:Audience></saml:AudienceRestriction>'
            '</saml:Conditions>'
            '<saml:Subject><saml:NameID>user@test.com</saml:NameID></saml:Subject>'
            '</saml:Assertion>'
            '</samlp:Response>'
        )
        b64 = base64.b64encode(xml.encode()).decode()

        # Use debug mode to allow fallback parser (testing the parser, not production gate)
        with patch("app.core.auth.saml.settings") as mock_settings:
            mock_settings.debug = True
            with pytest.raises(SAMLError, match="audience mismatch"):
                sp.parse_response(b64)


# ── Iteration 3 Tests ────────────────────────────────────────────────


class TestGETLoginEndpoint:
    """Verify that GET /api/v1/auth/login returns a usable response (not 405)."""

    @pytest.mark.asyncio
    async def test_get_login_returns_200(self, client: AsyncClient):
        """GET /api/v1/auth/login should return 200 with provider info."""
        resp = await client.get("/api/v1/auth/login")
        assert resp.status_code == 200
        data = resp.json()
        assert "providers" in data
        assert "login_endpoint" in data
        assert "message" in data

    @pytest.mark.asyncio
    async def test_get_login_preserves_redirect_url(self, client: AsyncClient):
        """GET /api/v1/auth/login?redirect_url=/foo passes redirect_url through."""
        resp = await client.get("/api/v1/auth/login", params={"redirect_url": "/dashboard"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["redirect_url"] == "/dashboard"

    @pytest.mark.asyncio
    async def test_browser_redirect_chain_does_not_end_in_405(self, client: AsyncClient):
        """Following browser redirect from a protected route should NOT yield 405."""
        # Step 1: hit a protected route as a browser
        resp = await client.get(
            "/api/v1/dashboard/summary",
            headers={"Accept": "text/html,application/xhtml+xml"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        location = resp.headers["Location"]
        assert "/auth/login" in location

        # Step 2: follow the redirect — should get 200, not 405
        resp2 = await client.get(location)
        assert resp2.status_code == 200
        assert resp2.json().get("message") is not None


class TestRefreshMalformedSub:
    """Verify that refresh endpoint handles malformed 'sub' claim gracefully."""

    @pytest.mark.asyncio
    async def test_refresh_with_non_uuid_sub_returns_401(self, client: AsyncClient):
        """A signed refresh token with non-UUID sub should return 401, not 500."""
        # Build a valid refresh token with a non-UUID sub
        payload = {
            "sub": "not-a-uuid",
            "type": "refresh",
            "jti": str(uuid.uuid4()),
            "exp": 9999999999,
            "iat": 1700000000,
        }
        bad_token = pyjwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)

        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": bad_token},
        )
        assert resp.status_code == 401
        assert "invalid" in resp.json()["detail"].lower() or "malformed" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_refresh_with_empty_sub_returns_401(self, client: AsyncClient):
        """A signed refresh token with empty sub should return 401."""
        payload = {
            "sub": "",
            "type": "refresh",
            "jti": str(uuid.uuid4()),
            "exp": 9999999999,
            "iat": 1700000000,
        }
        bad_token = pyjwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)

        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": bad_token},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_refresh_with_missing_sub_returns_401(self, client: AsyncClient):
        """A refresh token with no sub claim at all returns 401."""
        payload = {
            "type": "refresh",
            "jti": str(uuid.uuid4()),
            "exp": 9999999999,
            "iat": 1700000000,
        }
        bad_token = pyjwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)

        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": bad_token},
        )
        assert resp.status_code == 401


class TestVerifyRefreshTokenSubValidation:
    """Unit tests for verify_refresh_token sub claim validation in jwt.py."""

    def test_valid_uuid_sub_passes(self):
        """A refresh token with valid UUID sub is accepted."""
        user_id = uuid.uuid4()
        token = create_refresh_token(user_id=user_id)
        claims = verify_refresh_token(token)
        assert claims["sub"] == str(user_id)

    def test_non_uuid_sub_raises_invalid_token_error(self):
        """A refresh token with non-UUID sub raises InvalidTokenError."""
        payload = {
            "sub": "not-a-uuid",
            "type": "refresh",
            "jti": str(uuid.uuid4()),
            "exp": 9999999999,
            "iat": 1700000000,
        }
        token = pyjwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)
        with pytest.raises(InvalidTokenError, match="not a valid UUID"):
            verify_refresh_token(token)

    def test_missing_sub_raises_invalid_token_error(self):
        """A refresh token with no sub claim raises InvalidTokenError."""
        payload = {
            "type": "refresh",
            "jti": str(uuid.uuid4()),
            "exp": 9999999999,
            "iat": 1700000000,
        }
        token = pyjwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)
        with pytest.raises(InvalidTokenError, match="missing"):
            verify_refresh_token(token)


class TestSAMLIdPMetadata:
    """Verify SAML IdP metadata URL is wired and functional."""

    @pytest.mark.asyncio
    async def test_load_idp_metadata_populates_fields(self):
        """Loading IdP metadata populates entity_id, sso_url, and cert."""
        import httpx
        from unittest.mock import AsyncMock

        metadata_xml = (
            '<?xml version="1.0"?>'
            '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
            'entityID="https://idp.example.com">'
            '<md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
            '<md:KeyDescriptor>'
            '<ds:KeyInfo xmlns:ds="http://www.w3.org/2000/09/xmldsig#">'
            '<ds:X509Data><ds:X509Certificate>MIIBxTCCAW4fakecert</ds:X509Certificate></ds:X509Data>'
            '</ds:KeyInfo>'
            '</md:KeyDescriptor>'
            '<md:SingleSignOnService '
            'Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" '
            'Location="https://idp.example.com/sso"/>'
            '</md:IDPSSODescriptor>'
            '</md:EntityDescriptor>'
        )

        sp = SAMLServiceProvider(
            idp_metadata_url="https://idp.example.com/metadata",
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = metadata_xml
        mock_response.raise_for_status = MagicMock()

        with patch("app.core.resilience.resilient_http_get", new_callable=AsyncMock, return_value=mock_response):
            await sp.load_idp_metadata()

        assert sp.idp_entity_id == "https://idp.example.com"
        assert sp.idp_sso_url == "https://idp.example.com/sso"
        assert sp.idp_x509_cert == "MIIBxTCCAW4fakecert"
        assert sp._metadata_loaded is True

    @pytest.mark.asyncio
    async def test_load_idp_metadata_no_url_raises(self):
        """Loading metadata without URL configured raises SAMLError."""
        sp = SAMLServiceProvider(idp_metadata_url="")
        with pytest.raises(SAMLError, match="not configured"):
            await sp.load_idp_metadata()

    @pytest.mark.asyncio
    async def test_load_idp_metadata_does_not_overwrite_existing(self):
        """Metadata loading does not overwrite directly-configured values."""
        from unittest.mock import AsyncMock, MagicMock

        metadata_xml = (
            '<?xml version="1.0"?>'
            '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
            'entityID="https://metadata-idp.example.com">'
            '<md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
            '<md:SingleSignOnService '
            'Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" '
            'Location="https://metadata-idp.example.com/sso"/>'
            '</md:IDPSSODescriptor>'
            '</md:EntityDescriptor>'
        )

        sp = SAMLServiceProvider(
            idp_entity_id="https://direct-config-idp.example.com",
            idp_sso_url="https://direct-config-idp.example.com/sso",
            idp_metadata_url="https://idp.example.com/metadata",
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = metadata_xml
        mock_response.raise_for_status = MagicMock()

        with patch("app.core.resilience.resilient_http_get", new_callable=AsyncMock, return_value=mock_response):
            await sp.load_idp_metadata()

        # Direct config should NOT be overwritten by metadata
        assert sp.idp_entity_id == "https://direct-config-idp.example.com"
        assert sp.idp_sso_url == "https://direct-config-idp.example.com/sso"


class TestSAMLFailClosed:
    """Verify SAML fails closed in production mode (debug=False)."""

    def test_saml_rejects_fallback_in_production_no_cert(self):
        """In production mode, SAML rejects responses when no IdP cert is configured."""
        import base64

        xml = (
            '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
            'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">'
            '<samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>'
            '<saml:Assertion><saml:Subject><saml:NameID>user@test.com</saml:NameID></saml:Subject></saml:Assertion>'
            '</samlp:Response>'
        )
        b64 = base64.b64encode(xml.encode()).decode()

        sp = SAMLServiceProvider(idp_x509_cert="")

        with patch("app.core.auth.saml.settings") as mock_settings:
            mock_settings.debug = False
            with pytest.raises(SAMLError, match="signature validation unavailable"):
                sp.parse_response(b64)

    def test_saml_allows_fallback_in_debug_mode(self):
        """In debug mode, SAML allows fallback XML parsing without cert."""
        import base64

        xml = (
            '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
            'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">'
            '<samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>'
            '<saml:Assertion><saml:Subject><saml:NameID>user@test.com</saml:NameID></saml:Subject></saml:Assertion>'
            '</samlp:Response>'
        )
        b64 = base64.b64encode(xml.encode()).decode()

        sp = SAMLServiceProvider(idp_x509_cert="")

        with patch("app.core.auth.saml.settings") as mock_settings:
            mock_settings.debug = True
            result = sp.parse_response(b64)
            assert result["email"] == "user@test.com"


class TestOIDCFailClosed:
    """Verify OIDC fails closed in production mode (debug=False)."""

    @pytest.mark.asyncio
    async def test_oidc_rejects_unverified_decode_in_production(self):
        """In production mode, verify_id_token refuses to fall back to unverified decoding."""
        from app.core.auth.oidc import OIDCError

        provider = OIDCProvider(client_id="test")
        # No JWKS configured, no discovery doc

        # Create a dummy ID token
        dummy_token = pyjwt.encode(
            {"sub": "user123", "email": "user@test.com", "aud": "test", "iss": "test", "exp": 9999999999, "iat": 1700000000},
            "some-secret",
            algorithm="HS256",
        )

        with patch("app.core.auth.oidc.settings") as mock_settings:
            mock_settings.debug = False
            with pytest.raises(OIDCError, match="signature validation unavailable"):
                await provider.verify_id_token(dummy_token)

    @pytest.mark.asyncio
    async def test_oidc_allows_unverified_decode_in_debug(self):
        """In debug mode, verify_id_token falls back to unverified decoding."""
        provider = OIDCProvider(client_id="test")

        dummy_token = pyjwt.encode(
            {"sub": "user123", "email": "user@test.com", "aud": "test", "iss": "test", "exp": 9999999999, "iat": 1700000000},
            "some-secret",
            algorithm="HS256",
        )

        with patch("app.core.auth.oidc.settings") as mock_settings:
            mock_settings.debug = True
            claims = await provider.verify_id_token(dummy_token)
            assert claims["sub"] == "user123"


class TestOIDCPublicAccessors:
    """Verify OIDCProvider public accessor methods replace private attribute access."""

    def test_has_discovery_doc_false_initially(self):
        """has_discovery_doc is False before discover() is called."""
        provider = OIDCProvider(client_id="test")
        assert provider.has_discovery_doc is False

    def test_has_discovery_doc_true_after_set(self):
        """has_discovery_doc is True after setting discovery doc."""
        provider = OIDCProvider(client_id="test")
        provider.set_discovery_doc({"authorization_endpoint": "https://example.com/auth"})
        assert provider.has_discovery_doc is True

    def test_get_authorization_endpoint_returns_none_without_doc(self):
        """get_authorization_endpoint returns None without discovery doc."""
        provider = OIDCProvider(client_id="test")
        assert provider.get_authorization_endpoint() is None

    def test_get_authorization_endpoint_returns_url_from_doc(self):
        """get_authorization_endpoint returns URL from discovery doc."""
        provider = OIDCProvider(client_id="test")
        provider.set_discovery_doc({"authorization_endpoint": "https://idp.example.com/authorize"})
        assert provider.get_authorization_endpoint() == "https://idp.example.com/authorize"


# ── Iteration 4 Tests ────────────────────────────────────────────────


class TestOIDCAutoDiscover:
    """Verify OIDC auto-discovery on callback (Issue #1 fix)."""

    @pytest.mark.asyncio
    async def test_ensure_discovered_fetches_when_cache_empty(self):
        """ensure_discovered re-fetches when discovery cache is empty."""
        from unittest.mock import AsyncMock, MagicMock

        provider = OIDCProvider(
            client_id="test",
            discovery_url="https://idp.example.com/.well-known/openid-configuration",
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "authorization_endpoint": "https://idp.example.com/auth",
            "token_endpoint": "https://idp.example.com/token",
            "userinfo_endpoint": "https://idp.example.com/userinfo",
            "issuer": "https://idp.example.com",
        }
        mock_response.raise_for_status = MagicMock()

        with patch("app.core.resilience.resilient_http_get", new_callable=AsyncMock, return_value=mock_response):
            assert not provider.has_discovery_doc
            doc = await provider.ensure_discovered()
            assert doc is not None
            assert provider.has_discovery_doc
            assert doc["token_endpoint"] == "https://idp.example.com/token"

    @pytest.mark.asyncio
    async def test_ensure_discovered_returns_cached(self):
        """ensure_discovered returns cached doc without re-fetching."""
        provider = OIDCProvider(client_id="test")
        provider.set_discovery_doc({"token_endpoint": "https://cached.example.com/token"})

        doc = await provider.ensure_discovered()
        assert doc["token_endpoint"] == "https://cached.example.com/token"

    @pytest.mark.asyncio
    async def test_ensure_discovered_returns_none_without_url(self):
        """ensure_discovered returns None when no discovery URL configured."""
        provider = OIDCProvider(client_id="test", discovery_url="")
        doc = await provider.ensure_discovered()
        assert doc is None

    @pytest.mark.asyncio
    async def test_exchange_code_auto_discovers(self):
        """exchange_code auto-discovers if cache is empty."""
        from unittest.mock import AsyncMock, MagicMock
        from app.core.auth.oidc import OIDCError

        provider = OIDCProvider(
            client_id="test",
            client_secret="secret",
            discovery_url="https://idp.example.com/.well-known/openid-configuration",
            redirect_uri="https://app.example.com/callback",
        )

        discovery_response = MagicMock()
        discovery_response.status_code = 200
        discovery_response.json.return_value = {
            "token_endpoint": "https://idp.example.com/token",
            "issuer": "https://idp.example.com",
        }
        discovery_response.raise_for_status = MagicMock()

        token_response = MagicMock()
        token_response.status_code = 200
        token_response.json.return_value = {
            "access_token": "at-123",
            "id_token": "it-123",
        }
        token_response.raise_for_status = MagicMock()

        with patch("app.core.resilience.resilient_http_get", new_callable=AsyncMock, return_value=discovery_response), \
             patch("app.core.resilience.resilient_http_post", new_callable=AsyncMock, return_value=token_response):
            # Cache is empty — exchange_code should auto-discover
            result = await provider.exchange_code("auth-code-123")
            assert result["access_token"] == "at-123"
            assert provider.has_discovery_doc


class TestDBBackedOIDCState:
    """Verify DB-backed OIDC state storage (Issue #1 fix)."""

    @pytest.mark.asyncio
    async def test_db_store_and_validate(self, db_session):
        """State stored in DB can be validated and consumed."""
        from app.core.auth.oidc import db_store_oidc_state, db_validate_and_consume_oidc_state

        await db_store_oidc_state(db_session, "db-state-abc", "db-nonce-xyz")
        nonce = await db_validate_and_consume_oidc_state(db_session, "db-state-abc")
        assert nonce == "db-nonce-xyz"

    @pytest.mark.asyncio
    async def test_db_state_consumed_on_use(self, db_session):
        """State is consumed (deleted) after validation — replay rejected."""
        from app.core.auth.oidc import db_store_oidc_state, db_validate_and_consume_oidc_state

        await db_store_oidc_state(db_session, "db-consume-state", "nonce-123")
        nonce = await db_validate_and_consume_oidc_state(db_session, "db-consume-state")
        assert nonce == "nonce-123"

        # Second call should return None (consumed)
        nonce2 = await db_validate_and_consume_oidc_state(db_session, "db-consume-state")
        assert nonce2 is None

    @pytest.mark.asyncio
    async def test_db_validate_unknown_state_returns_none(self, db_session):
        """Unknown state returns None from DB store."""
        from app.core.auth.oidc import db_validate_and_consume_oidc_state

        result = await db_validate_and_consume_oidc_state(db_session, "nonexistent-db-state")
        assert result is None

    @pytest.mark.asyncio
    async def test_db_validate_falls_through_to_db_when_not_in_memory(self, db_session):
        """State stored only in DB (simulating different worker) is found."""
        from app.core.auth.oidc import (
            db_validate_and_consume_oidc_state,
            _oidc_state_store,
        )
        from app.models.oidc_state import OIDCStateEntry
        from datetime import datetime, timezone
        import uuid

        # Insert directly into DB (bypassing in-memory cache)
        entry = OIDCStateEntry(
            id=uuid.uuid4(),
            state="db-only-state",
            nonce="db-only-nonce",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(entry)
        await db_session.flush()

        # Ensure NOT in memory
        _oidc_state_store.pop("db-only-state", None)

        # Should find it in DB
        nonce = await db_validate_and_consume_oidc_state(db_session, "db-only-state")
        assert nonce == "db-only-nonce"


class TestRefreshTokenJTIRequired:
    """Verify refresh tokens without JTI are rejected (Issue #2 fix)."""

    def test_refresh_token_without_jti_rejected(self):
        """A signed refresh token missing JTI is rejected by verify_refresh_token."""
        payload = {
            "sub": str(uuid.uuid4()),
            "type": "refresh",
            "exp": 9999999999,
            "iat": 1700000000,
        }
        token = pyjwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)

        with pytest.raises(InvalidTokenError, match="jti"):
            verify_refresh_token(token)

    def test_refresh_token_with_empty_jti_rejected(self):
        """A signed refresh token with empty-string JTI is rejected."""
        payload = {
            "sub": str(uuid.uuid4()),
            "type": "refresh",
            "jti": "",
            "exp": 9999999999,
            "iat": 1700000000,
        }
        token = pyjwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)

        with pytest.raises(InvalidTokenError, match="jti"):
            verify_refresh_token(token)

    def test_refresh_token_with_whitespace_jti_rejected(self):
        """A signed refresh token with whitespace-only JTI is rejected."""
        payload = {
            "sub": str(uuid.uuid4()),
            "type": "refresh",
            "jti": "   ",
            "exp": 9999999999,
            "iat": 1700000000,
        }
        token = pyjwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)

        with pytest.raises(InvalidTokenError, match="jti"):
            verify_refresh_token(token)

    def test_refresh_token_with_valid_jti_accepted(self):
        """A proper refresh token with valid JTI passes verification."""
        from app.core.auth.jwt import create_refresh_token
        user_id = uuid.uuid4()
        token = create_refresh_token(user_id=user_id)
        claims = verify_refresh_token(token)
        assert claims["jti"]
        assert claims["sub"] == str(user_id)

    @pytest.mark.asyncio
    async def test_refresh_endpoint_rejects_no_jti_token(self, client: AsyncClient):
        """POST /refresh with a token missing JTI returns 401."""
        payload = {
            "sub": str(uuid.uuid4()),
            "type": "refresh",
            "exp": 9999999999,
            "iat": 1700000000,
        }
        bad_token = pyjwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)

        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": bad_token},
        )
        assert resp.status_code == 401


class TestMeReturnsRoles:
    """Verify /me returns roles as a list (Issue #3 fix)."""

    @pytest.mark.asyncio
    async def test_me_returns_roles_list(self, client: AsyncClient):
        """GET /me returns roles as a list, not a single string."""
        from app.core.auth.jwt import create_access_token

        token = create_access_token(
            user_id=uuid.uuid4(),
            email="roles-test@example.com",
            role="admin",
            full_name="Roles Test",
        )
        resp = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "roles" in data
        assert isinstance(data["roles"], list)
        assert "admin" in data["roles"]
        # Ensure old field is not present
        assert "role" not in data
