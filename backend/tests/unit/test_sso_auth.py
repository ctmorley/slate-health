"""Tests for Sprint 6: SSO Authentication (SAML/OIDC).

Covers:
- SAML SP: AuthnRequest creation, Response parsing, attribute extraction, user creation
- OIDC RP: Authorization URL creation, code exchange, token validation, user creation
- JWT: Token create/verify, expiry, refresh
- User provisioning: first login creates user, second login updates, role mapping
- Integration: protected endpoints return 401/403 appropriately
- Role mapping: IdP attribute → Slate Health role
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse, parse_qs

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.auth.jwt import (
    InvalidTokenError,
    TokenExpiredError,
    create_access_token,
    create_refresh_token,
    verify_refresh_token,
    verify_token,
)
from app.core.auth.oidc import OIDCError, OIDCProvider, store_oidc_state
from app.core.auth.saml import SAMLError, SAMLServiceProvider
from app.models.user import User
from app.services.user_service import map_role_from_idp, provision_user

SECRET = "change-me-in-production-use-at-least-32-bytes!"


# ── Helpers ───────────────────────────────────────────────────────────


def _build_saml_response(
    subject: str = "user@example.com",
    attributes: dict[str, list[str]] | None = None,
    status_code: str = "urn:oasis:names:tc:SAML:2.0:status:Success",
) -> str:
    """Build a minimal SAML Response XML and return base64-encoded."""
    attrs_xml = ""
    if attributes:
        attrs_xml = "<saml:AttributeStatement>"
        for name, values in attributes.items():
            vals_xml = "".join(
                f"<saml:AttributeValue>{v}</saml:AttributeValue>" for v in values
            )
            attrs_xml += (
                f'<saml:Attribute Name="{name}">{vals_xml}</saml:Attribute>'
            )
        attrs_xml += "</saml:AttributeStatement>"

    xml = (
        '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
        'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">'
        f'<samlp:Status><samlp:StatusCode Value="{status_code}"/></samlp:Status>'
        '<saml:Assertion>'
        f"<saml:Subject><saml:NameID>{subject}</saml:NameID></saml:Subject>"
        f'<saml:AuthnStatement SessionIndex="session-123"/>'
        f"{attrs_xml}"
        "</saml:Assertion>"
        "</samlp:Response>"
    )
    return base64.b64encode(xml.encode("utf-8")).decode("ascii")


def _build_oidc_id_token(claims: dict) -> str:
    """Build a fake unsigned JWT-like ID token for testing."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.fake-signature"


# ── SAML Tests ────────────────────────────────────────────────────────


class TestSAMLServiceProvider:
    """Tests for SAML 2.0 SP functionality.

    These tests exercise the fallback XML parser (no IdP cert configured),
    which requires debug mode since production mode fails closed.
    """

    @pytest.fixture(autouse=True)
    def _enable_debug_for_fallback(self):
        """Enable debug mode so SAML fallback parser is accessible for testing."""
        with patch("app.core.auth.saml.settings") as mock_settings:
            mock_settings.debug = True
            mock_settings.saml_sp_entity_id = settings.saml_sp_entity_id
            mock_settings.saml_sp_acs_url = settings.saml_sp_acs_url
            mock_settings.saml_idp_entity_id = settings.saml_idp_entity_id
            mock_settings.saml_idp_sso_url = settings.saml_idp_sso_url
            mock_settings.saml_idp_x509_cert = settings.saml_idp_x509_cert
            mock_settings.saml_idp_metadata_url = settings.saml_idp_metadata_url
            yield

    def test_create_authn_request(self):
        """AuthnRequest creation produces a redirect URL with SAMLRequest parameter."""
        sp = SAMLServiceProvider(
            sp_entity_id="https://sp.example.com",
            sp_acs_url="https://sp.example.com/acs",
            idp_sso_url="https://idp.example.com/sso",
        )
        result = sp.create_authn_request(relay_state="/dashboard")

        assert "url" in result
        assert "request_id" in result
        assert result["request_id"].startswith("_slate_")
        assert "SAMLRequest=" in result["url"]
        assert "RelayState=%2Fdashboard" in result["url"]
        assert result["url"].startswith("https://idp.example.com/sso?")

    def test_create_authn_request_no_idp_url(self):
        """AuthnRequest fails if IdP SSO URL is not configured."""
        sp = SAMLServiceProvider(idp_sso_url="")
        with pytest.raises(SAMLError, match="not configured"):
            sp.create_authn_request()

    def test_parse_response_success(self):
        """Valid SAML response is parsed correctly."""
        sp = SAMLServiceProvider()
        response_b64 = _build_saml_response(
            subject="alice@example.com",
            attributes={
                "displayName": ["Alice Smith"],
                "role": ["admin"],
                "email": ["alice@example.com"],
            },
        )
        result = sp.parse_response(response_b64)

        assert result["subject_id"] == "alice@example.com"
        assert result["email"] == "alice@example.com"
        assert "displayName" in result["attributes"]
        assert result["attributes"]["role"] == ["admin"]
        assert result["session_index"] == "session-123"

    def test_parse_response_failure_status(self):
        """SAML response with non-Success status raises SAMLError."""
        sp = SAMLServiceProvider()
        response_b64 = _build_saml_response(
            status_code="urn:oasis:names:tc:SAML:2.0:status:Requester",
        )
        with pytest.raises(SAMLError, match="not Success"):
            sp.parse_response(response_b64)

    def test_parse_response_no_assertion(self):
        """SAML response without Assertion raises SAMLError."""
        sp = SAMLServiceProvider()
        xml = (
            '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
            'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">'
            '<samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>'
            "</samlp:Response>"
        )
        b64 = base64.b64encode(xml.encode()).decode()
        with pytest.raises(SAMLError, match="No Assertion"):
            sp.parse_response(b64)

    def test_parse_response_invalid_base64(self):
        """Invalid base64 input raises SAMLError."""
        sp = SAMLServiceProvider()
        with pytest.raises(SAMLError, match="decode"):
            sp.parse_response("!!not-valid-base64!!")

    def test_parse_response_invalid_xml(self):
        """Non-XML content raises SAMLError."""
        sp = SAMLServiceProvider()
        b64 = base64.b64encode(b"this is not xml").decode()
        with pytest.raises(SAMLError, match="parse"):
            sp.parse_response(b64)

    def test_generate_sp_metadata(self):
        """SP metadata XML is well-formed and contains entity ID and ACS URL."""
        sp = SAMLServiceProvider(
            sp_entity_id="https://sp.example.com",
            sp_acs_url="https://sp.example.com/acs",
        )
        metadata = sp.generate_sp_metadata()

        assert "https://sp.example.com" in metadata
        assert "https://sp.example.com/acs" in metadata
        assert "SPSSODescriptor" in metadata
        assert "AssertionConsumerService" in metadata

    def test_parse_response_extracts_email_from_attributes(self):
        """Email is extracted from attributes when different from NameID."""
        sp = SAMLServiceProvider()
        response_b64 = _build_saml_response(
            subject="uid-12345",
            attributes={"email": ["bob@example.com"]},
        )
        result = sp.parse_response(response_b64)
        assert result["email"] == "bob@example.com"
        assert result["subject_id"] == "uid-12345"


# ── OIDC Tests ────────────────────────────────────────────────────────


class TestOIDCProvider:
    """Tests for OIDC Relying Party functionality."""

    def test_create_authorization_url(self):
        """Authorization URL is correctly constructed."""
        provider = OIDCProvider(
            client_id="test-client",
            redirect_uri="http://localhost:8000/callback",
            scopes="openid email profile",
        )
        result = provider.create_authorization_url(
            authorization_endpoint="https://idp.example.com/authorize",
        )

        assert "url" in result
        assert "state" in result
        assert "nonce" in result
        url = result["url"]
        assert "response_type=code" in url
        assert "client_id=test-client" in url
        assert "redirect_uri=" in url
        assert "scope=openid" in url
        assert f"state={result['state']}" in url

    def test_create_authorization_url_no_client_id(self):
        """Authorization URL creation fails without client_id."""
        provider = OIDCProvider(client_id="")
        with pytest.raises(OIDCError, match="client_id"):
            provider.create_authorization_url(authorization_endpoint="https://idp.example.com/auth")

    def test_create_authorization_url_no_endpoint(self):
        """Authorization URL creation fails without endpoint."""
        provider = OIDCProvider(client_id="test-client")
        with pytest.raises(OIDCError, match="endpoint"):
            provider.create_authorization_url()

    def test_create_authorization_url_from_discovery(self):
        """Authorization URL uses discovery document endpoint."""
        provider = OIDCProvider(client_id="test-client")
        provider.set_discovery_doc({
            "authorization_endpoint": "https://idp.example.com/auth",
            "token_endpoint": "https://idp.example.com/token",
        })
        result = provider.create_authorization_url()
        assert "https://idp.example.com/auth" in result["url"]

    @pytest.mark.asyncio
    async def test_exchange_code(self):
        """Code exchange calls token endpoint and returns tokens."""
        provider = OIDCProvider(
            client_id="test-client",
            client_secret="test-secret",
            redirect_uri="http://localhost/callback",
        )
        provider.set_discovery_doc({
            "token_endpoint": "https://idp.example.com/token",
        })

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "at-123",
            "id_token": "id-token-123",
            "token_type": "Bearer",
        }
        mock_response.raise_for_status = MagicMock()

        with patch("app.core.resilience.resilient_http_post", new_callable=AsyncMock, return_value=mock_response):
            result = await provider.exchange_code("auth-code-123")

        assert result["access_token"] == "at-123"
        assert result["id_token"] == "id-token-123"

    @pytest.mark.asyncio
    async def test_exchange_code_no_endpoint(self):
        """Code exchange fails without token endpoint."""
        provider = OIDCProvider(client_id="test-client", client_secret="secret")
        with pytest.raises(OIDCError, match="endpoint"):
            await provider.exchange_code("code")

    @pytest.mark.asyncio
    async def test_get_userinfo(self):
        """Userinfo endpoint returns user claims."""
        provider = OIDCProvider()
        provider.set_discovery_doc({
            "userinfo_endpoint": "https://idp.example.com/userinfo",
        })

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "sub": "user-123",
            "email": "alice@example.com",
            "name": "Alice",
        }
        mock_response.raise_for_status = MagicMock()

        with patch("app.core.resilience.resilient_http_get", new_callable=AsyncMock, return_value=mock_response):
            result = await provider.get_userinfo("access-token-123")

        assert result["email"] == "alice@example.com"
        assert result["sub"] == "user-123"

    def test_parse_id_token_unverified(self):
        """ID token claims are extracted correctly."""
        provider = OIDCProvider()
        claims = {"sub": "user-123", "email": "alice@example.com", "name": "Alice"}
        token = _build_oidc_id_token(claims)

        result = provider.parse_id_token_unverified(token)
        assert result["sub"] == "user-123"
        assert result["email"] == "alice@example.com"

    def test_parse_id_token_invalid(self):
        """Invalid ID token raises OIDCError."""
        provider = OIDCProvider()
        with pytest.raises(OIDCError, match="3 parts"):
            provider.parse_id_token_unverified("not-a-jwt")

    def test_set_discovery_doc(self):
        """Discovery doc can be manually set."""
        provider = OIDCProvider()
        doc = {"authorization_endpoint": "https://example.com/auth"}
        provider.set_discovery_doc(doc)
        assert provider._discovery_doc == doc

    @pytest.mark.asyncio
    async def test_discover_no_url(self):
        """Discovery fails without URL configured."""
        provider = OIDCProvider(discovery_url="")
        with pytest.raises(OIDCError, match="not configured"):
            await provider.discover()


# ── Role Mapping Tests ────────────────────────────────────────────────


class TestRoleMapping:
    """Tests for IdP attribute → Slate Health role mapping."""

    def test_admin_role_mapped(self):
        """IdP 'admin' attribute maps to admin role."""
        assert map_role_from_idp({"role": "admin"}) == "admin"

    def test_administrator_role_mapped(self):
        """IdP 'administrator' attribute maps to admin role."""
        assert map_role_from_idp({"role": "administrator"}) == "admin"

    def test_reviewer_role_mapped(self):
        """IdP 'reviewer' attribute maps to reviewer role."""
        assert map_role_from_idp({"role": "reviewer"}) == "reviewer"

    def test_staff_role_mapped(self):
        """IdP 'staff' attribute maps to reviewer role."""
        assert map_role_from_idp({"role": "staff"}) == "reviewer"

    def test_clinician_role_mapped(self):
        """IdP 'clinician' attribute maps to reviewer role."""
        assert map_role_from_idp({"role": "clinician"}) == "reviewer"

    def test_unknown_role_defaults_to_viewer(self):
        """Unknown IdP role defaults to viewer."""
        assert map_role_from_idp({"role": "unknown"}) == "viewer"

    def test_no_role_attribute_defaults_to_viewer(self):
        """Missing role attribute defaults to viewer."""
        assert map_role_from_idp({}) == "viewer"

    def test_list_valued_role(self):
        """List-valued role attribute is handled."""
        assert map_role_from_idp({"role": ["admin", "user"]}) == "admin"

    def test_case_insensitive_mapping(self):
        """Role mapping is case-insensitive."""
        assert map_role_from_idp({"role": "Admin"}) == "admin"
        assert map_role_from_idp({"role": "REVIEWER"}) == "reviewer"

    def test_alternative_attribute_names(self):
        """Alternative attribute names like 'Role', 'groups' are checked."""
        assert map_role_from_idp({"Role": "admin"}) == "admin"
        assert map_role_from_idp({"groups": "staff"}) == "reviewer"


# ── User Provisioning Tests ──────────────────────────────────────────


class TestUserProvisioning:
    """Tests for SSO user provisioning."""

    @pytest.mark.asyncio
    async def test_first_login_creates_user(self, db_session: AsyncSession):
        """First SSO login creates a new user in the database."""
        user, is_new = await provision_user(
            db_session,
            email="newuser@example.com",
            full_name="New User",
            sso_provider="saml",
            sso_subject_id="saml-uid-001",
            idp_attributes={"role": "reviewer"},
        )

        assert is_new is True
        assert user.email == "newuser@example.com"
        assert user.full_name == "New User"
        assert user.sso_provider == "saml"
        assert user.sso_subject_id == "saml-uid-001"
        assert user.role == "reviewer"
        assert user.last_login is not None
        assert user.is_active is True

    @pytest.mark.asyncio
    async def test_second_login_updates_existing_user(self, db_session: AsyncSession):
        """Second SSO login finds existing user and updates last_login."""
        # First login
        user1, is_new1 = await provision_user(
            db_session,
            email="returning@example.com",
            full_name="Return User",
            sso_provider="oidc",
            sso_subject_id="oidc-uid-001",
        )
        assert is_new1 is True
        first_login_time = user1.last_login

        # Second login
        user2, is_new2 = await provision_user(
            db_session,
            email="returning@example.com",
            full_name="Return User Updated",
            sso_provider="oidc",
            sso_subject_id="oidc-uid-001",
        )

        assert is_new2 is False
        assert user2.id == user1.id
        assert user2.full_name == "Return User Updated"
        # last_login should be updated
        assert user2.last_login is not None

    @pytest.mark.asyncio
    async def test_email_fallback_links_sso(self, db_session: AsyncSession):
        """Pre-existing user found by email is linked to SSO provider."""
        # Pre-create a user without SSO
        existing = User(
            id=uuid.uuid4(),
            email="preexisting@example.com",
            full_name="Pre-existing",
            role="admin",
            is_active=True,
        )
        db_session.add(existing)
        await db_session.flush()

        # SSO login with same email
        user, is_new = await provision_user(
            db_session,
            email="preexisting@example.com",
            full_name="Pre-existing",
            sso_provider="saml",
            sso_subject_id="saml-preexist-001",
        )

        assert is_new is False
        assert user.id == existing.id
        assert user.sso_provider == "saml"
        assert user.sso_subject_id == "saml-preexist-001"
        # Original role is preserved
        assert user.role == "admin"

    @pytest.mark.asyncio
    async def test_provisioned_user_role_from_idp(self, db_session: AsyncSession):
        """New user gets role mapped from IdP attributes."""
        user, _ = await provision_user(
            db_session,
            email="adminuser@example.com",
            full_name="Admin User",
            sso_provider="oidc",
            sso_subject_id="oidc-admin-001",
            idp_attributes={"role": "admin"},
        )
        assert user.role == "admin"

    @pytest.mark.asyncio
    async def test_provisioned_user_default_viewer(self, db_session: AsyncSession):
        """New user without role attribute defaults to viewer."""
        user, _ = await provision_user(
            db_session,
            email="defaultuser@example.com",
            full_name="Default User",
            sso_provider="saml",
            sso_subject_id="saml-default-001",
            idp_attributes={},
        )
        assert user.role == "viewer"


# ── Auth Route Integration Tests ─────────────────────────────────────


class TestAuthRoutes:
    """Integration tests for auth API endpoints."""

    @pytest.mark.asyncio
    async def test_login_saml(self, client: AsyncClient):
        """POST /login with SAML provider returns redirect URL."""
        from app.api.v1.auth import set_saml_sp

        mock_sp = SAMLServiceProvider(
            sp_entity_id="https://sp.test",
            sp_acs_url="https://sp.test/acs",
            idp_sso_url="https://idp.test/sso",
        )
        set_saml_sp(mock_sp)

        try:
            resp = await client.post(
                "/api/v1/auth/login",
                json={"provider": "saml"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["provider"] == "saml"
            assert "SAMLRequest" in data["redirect_url"]
            assert data["redirect_url"].startswith("https://idp.test/sso")
        finally:
            set_saml_sp(None)

    @pytest.mark.asyncio
    async def test_login_oidc(self, client: AsyncClient):
        """POST /login with OIDC provider returns redirect URL."""
        from app.api.v1.auth import set_oidc_provider

        mock_provider = OIDCProvider(
            client_id="test-client",
            redirect_uri="http://localhost/callback",
        )
        mock_provider.set_discovery_doc({
            "authorization_endpoint": "https://idp.test/authorize",
        })
        set_oidc_provider(mock_provider)

        try:
            resp = await client.post(
                "/api/v1/auth/login",
                json={"provider": "oidc"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["provider"] == "oidc"
            assert "https://idp.test/authorize" in data["redirect_url"]
            assert "client_id=test-client" in data["redirect_url"]
        finally:
            set_oidc_provider(None)

    @pytest.mark.asyncio
    async def test_login_unsupported_provider(self, client: AsyncClient):
        """POST /login with unsupported provider returns 400."""
        resp = await client.post(
            "/api/v1/auth/login",
            json={"provider": "unsupported"},
        )
        assert resp.status_code == 400
        assert "Unsupported" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_saml_callback_creates_user_and_issues_tokens(self, client: AsyncClient):
        """SAML callback creates user and returns JWT tokens."""
        from app.api.v1.auth import set_saml_sp

        # Mock the SP to return predictable parsed data
        mock_sp = MagicMock(spec=SAMLServiceProvider)
        mock_sp.parse_response.return_value = {
            "subject_id": "saml-user-123",
            "email": "saml-user@example.com",
            "attributes": {
                "displayName": ["SAML Test User"],
                "role": ["reviewer"],
            },
            "session_index": "session-abc",
        }
        set_saml_sp(mock_sp)

        try:
            resp = await client.post(
                "/api/v1/auth/callback/saml",
                data={
                    "SAMLResponse": "mock-saml-response",
                    "RelayState": "/dashboard",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 302
            location = resp.headers["location"]
            from urllib.parse import urlparse, parse_qs
            params = parse_qs(urlparse(location).query)
            assert "access_token" in params
            assert "refresh_token" in params

            # Verify the JWT contains correct claims
            payload = verify_token(params["access_token"][0])
            assert payload.email == "saml-user@example.com"
            assert payload.role == "reviewer"
            assert payload.full_name == "SAML Test User"
        finally:
            set_saml_sp(None)

    @pytest.mark.asyncio
    async def test_saml_callback_invalid_response(self, client: AsyncClient):
        """SAML callback with invalid response returns 401."""
        from app.api.v1.auth import set_saml_sp

        mock_sp = MagicMock(spec=SAMLServiceProvider)
        mock_sp.parse_response.side_effect = SAMLError("Invalid signature")
        set_saml_sp(mock_sp)

        try:
            resp = await client.post(
                "/api/v1/auth/callback/saml",
                data={"SAMLResponse": "bad-response"},
            )
            assert resp.status_code == 401
            assert "SAML" in resp.json()["detail"]
        finally:
            set_saml_sp(None)

    @pytest.mark.asyncio
    async def test_oidc_callback_creates_user_and_issues_tokens(self, client: AsyncClient):
        """OIDC callback exchanges code, creates user, and returns JWT tokens."""
        from app.api.v1.auth import set_oidc_provider

        mock_provider = MagicMock(spec=OIDCProvider)
        mock_provider.exchange_code = AsyncMock(return_value={
            "access_token": "oidc-at-123",
            "id_token": _build_oidc_id_token({
                "sub": "oidc-user-456",
                "email": "oidc-user@example.com",
                "name": "OIDC Test User",
                "role": "admin",
            }),
        })
        mock_provider.get_userinfo = AsyncMock(return_value={
            "sub": "oidc-user-456",
            "email": "oidc-user@example.com",
            "name": "OIDC Test User",
            "role": "admin",
        })
        set_oidc_provider(mock_provider)

        # Pre-register OIDC state for CSRF validation
        store_oidc_state("csrf-state", "test-nonce")

        try:
            resp = await client.get(
                "/api/v1/auth/callback/oidc",
                params={"code": "auth-code-xyz", "state": "csrf-state"},
                follow_redirects=False,
            )
            assert resp.status_code == 302
            location = resp.headers["location"]
            from urllib.parse import urlparse, parse_qs
            params = parse_qs(urlparse(location).query)
            assert "access_token" in params
            assert "refresh_token" in params

            # Verify the JWT contains correct claims
            payload = verify_token(params["access_token"][0])
            assert payload.email == "oidc-user@example.com"
            assert payload.role == "admin"
            assert payload.full_name == "OIDC Test User"
        finally:
            set_oidc_provider(None)

    @pytest.mark.asyncio
    async def test_oidc_callback_exchange_failure(self, client: AsyncClient):
        """OIDC callback returns 401 when code exchange fails."""
        from app.api.v1.auth import set_oidc_provider

        mock_provider = MagicMock(spec=OIDCProvider)
        mock_provider.exchange_code = AsyncMock(side_effect=OIDCError("invalid_grant"))
        set_oidc_provider(mock_provider)

        # Pre-register state for CSRF validation
        store_oidc_state("exchange-fail-state", "nonce")

        try:
            resp = await client.get(
                "/api/v1/auth/callback/oidc",
                params={"code": "bad-code", "state": "exchange-fail-state"},
            )
            assert resp.status_code == 401
            assert "OIDC" in resp.json()["detail"]
        finally:
            set_oidc_provider(None)

    @pytest.mark.asyncio
    async def test_refresh_issues_new_tokens(self, client: AsyncClient):
        """POST /refresh with valid refresh token issues new token pair."""
        user_id = uuid.uuid4()
        refresh = create_refresh_token(user_id=user_id)

        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["refresh_token"] != refresh  # New refresh token issued

    @pytest.mark.asyncio
    async def test_refresh_rejects_invalid_token(self, client: AsyncClient):
        """POST /refresh with invalid token returns 401."""
        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": "invalid-refresh-token"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_get_me_with_jwt(self, client: AsyncClient):
        """GET /me returns user profile from JWT claims."""
        user_id = uuid.uuid4()
        token = create_access_token(
            user_id=user_id,
            email="me@example.com",
            role="viewer",
            full_name="Me User",
        )
        resp = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "me@example.com"
        assert data["roles"] == ["viewer"]
        assert data["full_name"] == "Me User"

    @pytest.mark.asyncio
    async def test_get_me_requires_auth(self, client: AsyncClient):
        """GET /me without token returns 401."""
        resp = await client.get("/api/v1/auth/me")
        assert resp.status_code == 401


# ── Protected Route Tests ─────────────────────────────────────────────


class TestProtectedRoutes:
    """Integration tests verifying auth enforcement on existing routes."""

    @pytest.mark.asyncio
    async def test_agents_endpoint_returns_401_without_token(self, client: AsyncClient):
        """Agent routes require authentication."""
        resp = await client.get("/api/v1/agents/eligibility/tasks")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_agents_endpoint_returns_200_with_token(self, client: AsyncClient):
        """Agent routes allow access with valid token."""
        token = create_access_token(
            user_id=uuid.uuid4(),
            email="viewer@test.com",
            role="viewer",
            full_name="Viewer",
        )
        resp = await client.get(
            "/api/v1/agents/eligibility/tasks",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_admin_endpoint_returns_403_for_viewer(self, client: AsyncClient):
        """Admin routes return 403 for non-admin users."""
        token = create_access_token(
            user_id=uuid.uuid4(),
            email="viewer@test.com",
            role="viewer",
            full_name="Viewer",
        )
        resp = await client.get(
            "/api/v1/admin/settings",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_endpoint_returns_200_for_admin(self, client: AsyncClient):
        """Admin routes allow admin users."""
        token = create_access_token(
            user_id=uuid.uuid4(),
            email="admin@test.com",
            role="admin",
            full_name="Admin",
        )
        resp = await client.get(
            "/api/v1/admin/settings",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_reviews_endpoint_requires_auth(self, client: AsyncClient):
        """Reviews routes require authentication."""
        resp = await client.get("/api/v1/reviews")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_audit_endpoint_requires_auth(self, client: AsyncClient):
        """Audit routes require authentication."""
        resp = await client.get("/api/v1/audit/logs")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_workflows_endpoint_requires_auth(self, client: AsyncClient):
        """Workflow routes require authentication."""
        resp = await client.get("/api/v1/workflows")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_payers_endpoint_requires_auth(self, client: AsyncClient):
        """Payer routes require authentication."""
        resp = await client.get("/api/v1/payers")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_dashboard_endpoint_requires_auth(self, client: AsyncClient):
        """Dashboard routes require authentication."""
        resp = await client.get("/api/v1/dashboard/summary")
        assert resp.status_code == 401


# ── SAML + User Provisioning End-to-End ──────────────────────────────


class TestSAMLEndToEnd:
    """End-to-end SAML flow tests including DB provisioning."""

    @pytest.mark.asyncio
    async def test_saml_flow_provisions_user_with_correct_role(self, client: AsyncClient):
        """Full SAML flow: callback → user created with correct role → tokens issued."""
        from app.api.v1.auth import set_saml_sp

        mock_sp = MagicMock(spec=SAMLServiceProvider)
        mock_sp.parse_response.return_value = {
            "subject_id": "e2e-saml-user",
            "email": "e2e-saml@example.com",
            "attributes": {
                "displayName": ["E2E SAML User"],
                "role": ["staff"],  # maps to 'reviewer'
            },
            "session_index": "session-e2e",
        }
        set_saml_sp(mock_sp)

        try:
            resp = await client.post(
                "/api/v1/auth/callback/saml",
                data={"SAMLResponse": "mock-response"},
                follow_redirects=False,
            )
            assert resp.status_code == 302
            params = parse_qs(urlparse(resp.headers["location"]).query)
            token = params["access_token"][0]

            # Use the token to access /me
            me_resp = await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert me_resp.status_code == 200
            profile = me_resp.json()
            assert profile["email"] == "e2e-saml@example.com"
            assert profile["roles"] == ["reviewer"]
            assert profile["full_name"] == "E2E SAML User"
        finally:
            set_saml_sp(None)

    @pytest.mark.asyncio
    async def test_subsequent_login_no_duplicate(self, client: AsyncClient):
        """Second SAML login for same user updates timestamp, no duplicate."""
        from app.api.v1.auth import set_saml_sp

        mock_sp = MagicMock(spec=SAMLServiceProvider)
        mock_sp.parse_response.return_value = {
            "subject_id": "repeat-saml-user",
            "email": "repeat-saml@example.com",
            "attributes": {"displayName": ["Repeat User"]},
            "session_index": "session-1",
        }
        set_saml_sp(mock_sp)

        try:
            # First login
            resp1 = await client.post(
                "/api/v1/auth/callback/saml",
                data={"SAMLResponse": "mock-1"},
                follow_redirects=False,
            )
            assert resp1.status_code == 302
            params1 = parse_qs(urlparse(resp1.headers["location"]).query)
            payload1 = verify_token(params1["access_token"][0])

            # Second login
            resp2 = await client.post(
                "/api/v1/auth/callback/saml",
                data={"SAMLResponse": "mock-2"},
                follow_redirects=False,
            )
            assert resp2.status_code == 302
            params2 = parse_qs(urlparse(resp2.headers["location"]).query)
            payload2 = verify_token(params2["access_token"][0])

            # Same user ID — no duplicate created
            assert payload1.user_id == payload2.user_id
        finally:
            set_saml_sp(None)


# ── OIDC End-to-End ──────────────────────────────────────────────────


class TestOIDCEndToEnd:
    """End-to-end OIDC flow tests."""

    @pytest.mark.asyncio
    async def test_oidc_flow_provisions_user(self, client: AsyncClient):
        """Full OIDC flow: callback → user created → tokens issued."""
        from app.api.v1.auth import set_oidc_provider

        mock_provider = MagicMock(spec=OIDCProvider)
        mock_provider.exchange_code = AsyncMock(return_value={
            "access_token": "oidc-at",
            "id_token": "unused",
        })
        mock_provider.get_userinfo = AsyncMock(return_value={
            "sub": "e2e-oidc-user",
            "email": "e2e-oidc@example.com",
            "name": "E2E OIDC User",
            "role": "admin",
        })
        set_oidc_provider(mock_provider)

        # Pre-register state for CSRF validation
        store_oidc_state("e2e-state", "e2e-nonce")

        try:
            resp = await client.get(
                "/api/v1/auth/callback/oidc",
                params={"code": "e2e-code", "state": "e2e-state"},
                follow_redirects=False,
            )
            assert resp.status_code == 302
            params = parse_qs(urlparse(resp.headers["location"]).query)
            token = params["access_token"][0]

            # Use the token to access /me
            me_resp = await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert me_resp.status_code == 200
            profile = me_resp.json()
            assert profile["email"] == "e2e-oidc@example.com"
            assert profile["roles"] == ["admin"]
        finally:
            set_oidc_provider(None)

    @pytest.mark.asyncio
    async def test_oidc_callback_no_email_returns_401(self, client: AsyncClient):
        """OIDC callback without email in userinfo returns 401."""
        from app.api.v1.auth import set_oidc_provider

        mock_provider = MagicMock(spec=OIDCProvider)
        mock_provider.exchange_code = AsyncMock(return_value={
            "access_token": "at-no-email",
        })
        mock_provider.get_userinfo = AsyncMock(return_value={
            "sub": "no-email-user",
            # No email claim
        })
        set_oidc_provider(mock_provider)

        # Pre-register state for CSRF validation
        store_oidc_state("no-email-state", "nonce")

        try:
            resp = await client.get(
                "/api/v1/auth/callback/oidc",
                params={"code": "code", "state": "no-email-state"},
            )
            assert resp.status_code == 401
            assert "Email" in resp.json()["detail"]
        finally:
            set_oidc_provider(None)


# ── Refresh Token Reuse Rejection Tests ─────────────────────────────


class TestRefreshTokenRevocation:
    """Tests verifying refresh token rotation and reuse rejection."""

    @pytest.mark.asyncio
    async def test_refresh_token_reuse_rejected(self, client: AsyncClient):
        """Using the same refresh token twice should fail on second use."""
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

        # Second use of the SAME refresh token — should be rejected
        resp2 = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh},
        )
        assert resp2.status_code == 401
        assert "revoked" in resp2.json()["detail"].lower() or "already used" in resp2.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_refresh_token_rotation_chain(self, client: AsyncClient):
        """New refresh token from rotation should work, old one should not."""
        user_id = uuid.uuid4()
        refresh = create_refresh_token(user_id=user_id)

        # First refresh
        resp1 = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh},
        )
        assert resp1.status_code == 200
        new_refresh = resp1.json()["refresh_token"]

        # Old refresh token rejected
        resp2 = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh},
        )
        assert resp2.status_code == 401

        # New refresh token works
        resp3 = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": new_refresh},
        )
        assert resp3.status_code == 200


# ── OIDC State Validation Tests ─────────────────────────────────────


class TestOIDCStateValidation:
    """Tests verifying OIDC state parameter validation for CSRF protection."""

    @pytest.mark.asyncio
    async def test_oidc_callback_rejects_invalid_state(self, client: AsyncClient):
        """OIDC callback rejects requests with unregistered state parameter."""
        from app.api.v1.auth import set_oidc_provider

        mock_provider = MagicMock(spec=OIDCProvider)
        mock_provider.exchange_code = AsyncMock(return_value={"access_token": "at"})
        mock_provider.get_userinfo = AsyncMock(return_value={
            "sub": "user", "email": "user@example.com", "name": "User",
        })
        set_oidc_provider(mock_provider)

        try:
            resp = await client.get(
                "/api/v1/auth/callback/oidc",
                params={"code": "code", "state": "unregistered-state"},
            )
            assert resp.status_code == 401
            assert "state" in resp.json()["detail"].lower()
        finally:
            set_oidc_provider(None)

    @pytest.mark.asyncio
    async def test_oidc_callback_rejects_missing_state(self, client: AsyncClient):
        """OIDC callback rejects requests without state parameter."""
        from app.api.v1.auth import set_oidc_provider

        mock_provider = MagicMock(spec=OIDCProvider)
        set_oidc_provider(mock_provider)

        try:
            resp = await client.get(
                "/api/v1/auth/callback/oidc",
                params={"code": "code"},
            )
            assert resp.status_code == 401
            assert "state" in resp.json()["detail"].lower()
        finally:
            set_oidc_provider(None)

    @pytest.mark.asyncio
    async def test_oidc_state_consumed_on_use(self, client: AsyncClient):
        """OIDC state is consumed after use — replay is rejected."""
        from app.api.v1.auth import set_oidc_provider

        mock_provider = MagicMock(spec=OIDCProvider)
        mock_provider.exchange_code = AsyncMock(return_value={"access_token": "at"})
        mock_provider.get_userinfo = AsyncMock(return_value={
            "sub": "user", "email": "replay@example.com", "name": "Replay User",
        })
        set_oidc_provider(mock_provider)

        store_oidc_state("replay-state", "nonce")

        try:
            # First use succeeds (302 redirect with tokens)
            resp1 = await client.get(
                "/api/v1/auth/callback/oidc",
                params={"code": "code", "state": "replay-state"},
                follow_redirects=False,
            )
            assert resp1.status_code == 302

            # Same state replayed — rejected
            resp2 = await client.get(
                "/api/v1/auth/callback/oidc",
                params={"code": "code", "state": "replay-state"},
            )
            assert resp2.status_code == 401
        finally:
            set_oidc_provider(None)


# ── Login Redirect & Location Header Tests ──────────────────────────


class TestLoginRedirect:
    """Tests verifying login redirect behavior for unauthenticated requests."""

    @pytest.mark.asyncio
    async def test_unauthenticated_api_request_has_location_header(self, client: AsyncClient):
        """Unauthenticated API request returns 401 with Location header."""
        resp = await client.get("/api/v1/dashboard/summary")
        assert resp.status_code == 401
        assert "Location" in resp.headers
        assert "/api/v1/auth/login" in resp.headers["Location"]

    @pytest.mark.asyncio
    async def test_unauthenticated_browser_request_redirects(self, client: AsyncClient):
        """Unauthenticated browser request is redirected to login page."""
        resp = await client.get(
            "/api/v1/dashboard/summary",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "/api/v1/auth/login" in resp.headers["Location"]

    @pytest.mark.asyncio
    async def test_public_paths_not_redirected(self, client: AsyncClient):
        """Public paths (health, login, callback) don't require auth."""
        resp = await client.get("/health")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_authenticated_request_no_redirect(self, client: AsyncClient):
        """Authenticated API request is not redirected."""
        token = create_access_token(
            user_id=uuid.uuid4(),
            email="authed@test.com",
            role="viewer",
            full_name="Authed User",
        )
        resp = await client.get(
            "/api/v1/dashboard/summary",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert "Location" not in resp.headers


# ── WebSocket Authentication Tests ──────────────────────────────────


class TestWebSocketAuth:
    """Tests verifying WebSocket endpoint authentication enforcement."""

    @pytest.mark.asyncio
    async def test_websocket_rejects_unauthenticated(self, client: AsyncClient):
        """WebSocket connection without token is rejected."""
        from starlette.testclient import TestClient
        from app.main import create_app

        app = create_app()
        sync_client = TestClient(app)

        # Attempt WebSocket connection without token
        with pytest.raises(Exception):
            with sync_client.websocket_connect("/api/v1/ws/events"):
                pass  # Should not reach here

    @pytest.mark.asyncio
    async def test_websocket_accepts_authenticated(self, client: AsyncClient):
        """WebSocket connection with valid token is accepted."""
        from starlette.testclient import TestClient
        from app.main import create_app

        app = create_app()
        sync_client = TestClient(app)

        token = create_access_token(
            user_id=uuid.uuid4(),
            email="ws@test.com",
            role="viewer",
            full_name="WS User",
        )

        with sync_client.websocket_connect(f"/api/v1/ws/events?token={token}") as ws:
            ws.send_text("ping")
            data = ws.receive_text()
            import json
            assert json.loads(data)["event"] == "pong"
