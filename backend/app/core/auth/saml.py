"""SAML 2.0 Service Provider implementation.

Handles SP metadata generation, AuthnRequest creation, Response validation,
and attribute extraction. Integrates python3-saml (OneLogin) for cryptographic
signature validation when the IdP certificate is configured, with a fallback
XML parser for environments where xmlsec is unavailable.
"""

from __future__ import annotations

import base64
import logging
import uuid
import zlib
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Try to import python3-saml for proper signature validation
try:
    from onelogin.saml2.auth import OneLogin_Saml2_Auth
    from onelogin.saml2.response import OneLogin_Saml2_Response
    from onelogin.saml2.settings import OneLogin_Saml2_Settings
    from onelogin.saml2.utils import OneLogin_Saml2_Utils

    HAS_PYTHON3_SAML = True
    logger.info("python3-saml loaded — SAML signature validation enabled")
except ImportError:
    HAS_PYTHON3_SAML = False
    logger.warning(
        "python3-saml not available — SAML responses will use fallback XML parsing "
        "without cryptographic signature validation. Install python3-saml for production use."
    )

# SAML XML namespaces
NS_SAML = "urn:oasis:names:tc:SAML:2.0:assertion"
NS_SAMLP = "urn:oasis:names:tc:SAML:2.0:protocol"
NS_DS = "http://www.w3.org/2000/09/xmldsig#"

# Register namespaces for cleaner XML output
ET.register_namespace("saml", NS_SAML)
ET.register_namespace("samlp", NS_SAMLP)
ET.register_namespace("ds", NS_DS)


class SAMLError(Exception):
    """Raised for SAML-related errors."""

    pass


class SAMLServiceProvider:
    """SAML 2.0 Service Provider that creates AuthnRequests and validates Responses.

    When python3-saml is available and the IdP X.509 certificate is configured,
    responses are cryptographically validated. Otherwise falls back to XML
    parsing (suitable for development/testing only).
    """

    def __init__(
        self,
        sp_entity_id: str | None = None,
        sp_acs_url: str | None = None,
        idp_entity_id: str | None = None,
        idp_sso_url: str | None = None,
        idp_x509_cert: str | None = None,
        idp_metadata_url: str | None = None,
    ) -> None:
        self.sp_entity_id = sp_entity_id or settings.saml_sp_entity_id
        self.sp_acs_url = sp_acs_url or settings.saml_sp_acs_url
        self.idp_entity_id = idp_entity_id or settings.saml_idp_entity_id
        self.idp_sso_url = idp_sso_url or settings.saml_idp_sso_url
        self.idp_x509_cert = idp_x509_cert or settings.saml_idp_x509_cert
        self.idp_metadata_url = idp_metadata_url or settings.saml_idp_metadata_url
        self._metadata_loaded = False

    async def load_idp_metadata(self) -> None:
        """Fetch and parse IdP metadata from the configured metadata URL.

        Populates idp_entity_id, idp_sso_url, and idp_x509_cert from the
        IdP's metadata XML document. Results are cached after first successful fetch.

        Raises:
            SAMLError: If the metadata URL is not configured, fetch fails,
                       or required elements are missing from the metadata.
        """
        if self._metadata_loaded:
            return

        if not self.idp_metadata_url:
            raise SAMLError("SAML IdP metadata URL is not configured")

        try:
            from app.core.resilience import resilient_http_get
            resp = await resilient_http_get(self.idp_metadata_url, timeout=15.0)
            resp.raise_for_status()
            metadata_xml = resp.text
        except httpx.HTTPError as exc:
            raise SAMLError(f"Failed to fetch SAML IdP metadata from {self.idp_metadata_url}: {exc}")

        try:
            root = ET.fromstring(metadata_xml)
        except ET.ParseError as exc:
            raise SAMLError(f"Failed to parse SAML IdP metadata XML: {exc}")

        # Namespace map for SAML metadata
        ns_md = "urn:oasis:names:tc:SAML:2.0:metadata"

        # Extract entityID from the root EntityDescriptor
        entity_id = root.get("entityID")
        if entity_id and not self.idp_entity_id:
            self.idp_entity_id = entity_id
            logger.info("SAML IdP entityID from metadata: %s", entity_id)

        # Extract SingleSignOnService URL (HTTP-Redirect preferred, POST fallback)
        sso_descriptors = root.findall(f".//{{{ns_md}}}IDPSSODescriptor/{{{ns_md}}}SingleSignOnService")
        if not sso_descriptors:
            # Try direct children
            sso_descriptors = root.findall(f".//{{{ns_md}}}SingleSignOnService")

        sso_url = None
        for sso_elem in sso_descriptors:
            binding = sso_elem.get("Binding", "")
            location = sso_elem.get("Location", "")
            if "HTTP-Redirect" in binding and location:
                sso_url = location
                break
            if "HTTP-POST" in binding and location and not sso_url:
                sso_url = location

        if sso_url and not self.idp_sso_url:
            self.idp_sso_url = sso_url
            logger.info("SAML IdP SSO URL from metadata: %s", sso_url)

        # Extract X.509 certificate from KeyDescriptor
        cert_elem = root.find(
            f".//{{{ns_md}}}IDPSSODescriptor/{{{ns_md}}}KeyDescriptor"
            f"/{{{NS_DS}}}KeyInfo/{{{NS_DS}}}X509Data/{{{NS_DS}}}X509Certificate"
        )
        if cert_elem is None:
            # Try without IDPSSODescriptor wrapper
            cert_elem = root.find(
                f".//{{{NS_DS}}}KeyInfo/{{{NS_DS}}}X509Data/{{{NS_DS}}}X509Certificate"
            )

        if cert_elem is not None and cert_elem.text:
            cert_text = cert_elem.text.strip().replace("\n", "").replace("\r", "").replace(" ", "")
            if cert_text and not self.idp_x509_cert:
                self.idp_x509_cert = cert_text
                logger.info("SAML IdP X.509 certificate loaded from metadata (%d chars)", len(cert_text))

        self._metadata_loaded = True
        logger.info(
            "SAML IdP metadata loaded: entity_id=%s, sso_url=%s, cert=%s",
            self.idp_entity_id or "(not found)",
            self.idp_sso_url or "(not found)",
            "present" if self.idp_x509_cert else "(not found)",
        )

    def _get_saml_settings(self) -> dict[str, Any]:
        """Build python3-saml settings dict from configuration."""
        return {
            "strict": True,
            "debug": settings.debug,
            "sp": {
                "entityId": self.sp_entity_id,
                "assertionConsumerService": {
                    "url": self.sp_acs_url,
                    "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
                },
                "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
            },
            "idp": {
                "entityId": self.idp_entity_id,
                "singleSignOnService": {
                    "url": self.idp_sso_url,
                    "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
                },
                "x509cert": self.idp_x509_cert,
            },
        }

    def create_authn_request(self, relay_state: str | None = None) -> dict[str, str]:
        """Create a SAML AuthnRequest and return redirect parameters.

        Returns:
            Dict with 'url' (full redirect URL) and 'request_id'.
        """
        if not self.idp_sso_url:
            raise SAMLError("SAML IdP SSO URL is not configured")

        request_id = f"_slate_{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Build AuthnRequest XML
        authn_request = (
            f'<samlp:AuthnRequest xmlns:samlp="{NS_SAMLP}" '
            f'xmlns:saml="{NS_SAML}" '
            f'ID="{request_id}" '
            f'Version="2.0" '
            f'IssueInstant="{now}" '
            f'Destination="{self.idp_sso_url}" '
            f'AssertionConsumerServiceURL="{self.sp_acs_url}" '
            f'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST">'
            f"<saml:Issuer>{self.sp_entity_id}</saml:Issuer>"
            f'<samlp:NameIDPolicy Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress" '
            f'AllowCreate="true"/>'
            f"</samlp:AuthnRequest>"
        )

        # Deflate + Base64 encode for HTTP-Redirect binding
        deflated = zlib.compress(authn_request.encode("utf-8"))[2:-4]  # raw deflate
        encoded = base64.b64encode(deflated).decode("ascii")

        params = {"SAMLRequest": encoded}
        if relay_state:
            params["RelayState"] = relay_state

        separator = "&" if "?" in self.idp_sso_url else "?"
        redirect_url = f"{self.idp_sso_url}{separator}{urlencode(params)}"

        logger.info("SAML AuthnRequest created: id=%s, destination=%s", request_id, self.idp_sso_url)

        return {
            "url": redirect_url,
            "request_id": request_id,
        }

    def parse_response(self, saml_response_b64: str) -> dict[str, Any]:
        """Parse and validate a SAML Response from the IdP.

        When python3-saml is available and the IdP X.509 certificate is configured,
        performs full cryptographic signature validation. Otherwise falls back to
        XML parsing with structural checks only.

        Args:
            saml_response_b64: Base64-encoded SAML Response XML.

        Returns:
            Dict with 'subject_id', 'email', 'attributes', 'session_index'.

        Raises:
            SAMLError: If the response is invalid or assertion extraction fails.
        """
        # Try python3-saml validation first if available and cert is configured
        if HAS_PYTHON3_SAML and self.idp_x509_cert:
            return self._parse_with_python3_saml(saml_response_b64)

        # In production mode (debug=False), refuse to process SAML responses
        # without proper signature validation — fail closed.
        if not settings.debug:
            if not HAS_PYTHON3_SAML:
                raise SAMLError(
                    "SAML signature validation unavailable: python3-saml is not installed. "
                    "Install python3-saml for production use, or set SLATE_DEBUG=true for development."
                )
            if not self.idp_x509_cert:
                raise SAMLError(
                    "SAML signature validation unavailable: IdP X.509 certificate is not configured. "
                    "Set SLATE_SAML_IDP_X509_CERT for production use, or set SLATE_DEBUG=true for development."
                )

        # Development/debug mode only: allow fallback without signature validation
        if not HAS_PYTHON3_SAML:
            logger.warning(
                "DEBUG MODE: python3-saml not available — using fallback XML parsing without "
                "signature validation. This is NOT suitable for production."
            )
        elif not self.idp_x509_cert:
            logger.warning(
                "DEBUG MODE: IdP X.509 certificate not configured — skipping signature validation. "
                "Configure saml_idp_x509_cert for production use."
            )

        return self._parse_response_fallback(saml_response_b64)

    def _parse_with_python3_saml(self, saml_response_b64: str) -> dict[str, Any]:
        """Parse SAML response using python3-saml with full signature validation."""
        try:
            saml_settings = OneLogin_Saml2_Settings(
                self._get_saml_settings(), sp_validation_only=True
            )

            # Build the request data python3-saml expects
            request_data = {
                "https": "on",
                "http_host": "localhost",
                "script_name": "/api/v1/auth/callback/saml",
                "post_data": {"SAMLResponse": saml_response_b64},
            }

            # Parse URL from ACS URL for accurate request_data
            from urllib.parse import urlparse

            parsed_acs = urlparse(self.sp_acs_url)
            request_data["https"] = "on" if parsed_acs.scheme == "https" else "off"
            request_data["http_host"] = parsed_acs.netloc
            request_data["script_name"] = parsed_acs.path

            auth = OneLogin_Saml2_Auth(request_data, old_settings=saml_settings)
            auth.process_response()

            errors = auth.get_errors()
            if errors:
                error_reason = auth.get_last_error_reason() or ", ".join(errors)
                raise SAMLError(f"SAML response validation failed: {error_reason}")

            # Extract attributes from validated response
            name_id = auth.get_nameid()
            if not name_id:
                raise SAMLError("No NameID found in validated SAML assertion")

            attributes = auth.get_attributes()
            session_index = auth.get_session_index() or ""

            # Derive email
            email = name_id
            for attr_key in ("email",
                             "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
                             "urn:oid:0.9.2342.19200300.100.1.3"):
                if attr_key in attributes and attributes[attr_key]:
                    email = attributes[attr_key][0]
                    break

            logger.info(
                "SAML response validated via python3-saml: subject=%s, attributes=%d",
                name_id, len(attributes),
            )

            return {
                "subject_id": name_id,
                "email": email,
                "attributes": attributes,
                "session_index": session_index,
            }

        except SAMLError:
            raise
        except Exception as exc:
            raise SAMLError(f"SAML response validation failed: {exc}")

    def _parse_response_fallback(self, saml_response_b64: str) -> dict[str, Any]:
        """Fallback XML parsing when python3-saml is not available.

        Performs structural validation only — no cryptographic signature checks.
        """
        try:
            xml_bytes = base64.b64decode(saml_response_b64)
            xml_str = xml_bytes.decode("utf-8")
        except Exception as exc:
            raise SAMLError(f"Failed to decode SAML response: {exc}")

        try:
            root = ET.fromstring(xml_str)
        except ET.ParseError as exc:
            raise SAMLError(f"Failed to parse SAML response XML: {exc}")

        # Check status
        status_code_elem = root.find(f".//{{{NS_SAMLP}}}StatusCode")
        if status_code_elem is not None:
            status_value = status_code_elem.get("Value", "")
            if "Success" not in status_value:
                raise SAMLError(f"SAML response status is not Success: {status_value}")

        # Extract assertion
        assertion = root.find(f".//{{{NS_SAML}}}Assertion")
        if assertion is None:
            raise SAMLError("No Assertion found in SAML response")

        # Validate conditions if present (audience, time bounds)
        conditions = assertion.find(f".//{{{NS_SAML}}}Conditions")
        if conditions is not None:
            not_before = conditions.get("NotBefore")
            not_on_or_after = conditions.get("NotOnOrAfter")
            now = datetime.now(timezone.utc)

            if not_before:
                try:
                    nb_dt = datetime.fromisoformat(not_before.replace("Z", "+00:00"))
                    if now < nb_dt:
                        raise SAMLError(f"SAML assertion not yet valid (NotBefore: {not_before})")
                except ValueError:
                    pass

            if not_on_or_after:
                try:
                    noa_dt = datetime.fromisoformat(not_on_or_after.replace("Z", "+00:00"))
                    if now >= noa_dt:
                        raise SAMLError(f"SAML assertion has expired (NotOnOrAfter: {not_on_or_after})")
                except ValueError:
                    pass

            # Check audience restriction
            audience_elem = conditions.find(
                f".//{{{NS_SAML}}}AudienceRestriction/{{{NS_SAML}}}Audience"
            )
            if audience_elem is not None and audience_elem.text:
                expected_audience = self.sp_entity_id
                if audience_elem.text.strip() != expected_audience:
                    raise SAMLError(
                        f"SAML audience mismatch: expected '{expected_audience}', "
                        f"got '{audience_elem.text.strip()}'"
                    )

        # Validate recipient in SubjectConfirmationData
        subj_conf_data = assertion.find(
            f".//{{{NS_SAML}}}SubjectConfirmation/{{{NS_SAML}}}SubjectConfirmationData"
        )
        if subj_conf_data is not None:
            recipient = subj_conf_data.get("Recipient", "")
            if recipient and recipient != self.sp_acs_url:
                raise SAMLError(
                    f"SAML Recipient mismatch: expected '{self.sp_acs_url}', got '{recipient}'"
                )

        # Extract NameID (subject)
        name_id_elem = assertion.find(f".//{{{NS_SAML}}}NameID")
        if name_id_elem is None or not name_id_elem.text:
            raise SAMLError("No NameID found in SAML assertion")
        subject_id = name_id_elem.text.strip()

        # Extract attributes
        attributes: dict[str, list[str]] = {}
        attr_statements = assertion.findall(f".//{{{NS_SAML}}}AttributeStatement/{{{NS_SAML}}}Attribute")
        for attr_elem in attr_statements:
            attr_name = attr_elem.get("Name", "")
            values = []
            for val_elem in attr_elem.findall(f"{{{NS_SAML}}}AttributeValue"):
                if val_elem.text:
                    values.append(val_elem.text.strip())
            if attr_name and values:
                attributes[attr_name] = values

        # Extract session index
        authn_stmt = assertion.find(f".//{{{NS_SAML}}}AuthnStatement")
        session_index = authn_stmt.get("SessionIndex", "") if authn_stmt is not None else ""

        # Derive email — try NameID first, then email attribute
        email = subject_id
        for attr_key in ("email", "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
                         "urn:oid:0.9.2342.19200300.100.1.3"):
            if attr_key in attributes:
                email = attributes[attr_key][0]
                break

        logger.info("SAML response parsed (fallback): subject=%s, attributes=%d", subject_id, len(attributes))

        return {
            "subject_id": subject_id,
            "email": email,
            "attributes": attributes,
            "session_index": session_index,
        }

    def generate_sp_metadata(self) -> str:
        """Generate SAML SP metadata XML.

        Uses python3-saml metadata builder when available, falls back to
        manual XML construction.

        Returns:
            SP metadata XML string.
        """
        if HAS_PYTHON3_SAML and self.idp_x509_cert:
            try:
                saml_settings = OneLogin_Saml2_Settings(
                    self._get_saml_settings(), sp_validation_only=True
                )
                return saml_settings.get_sp_metadata()
            except Exception:
                pass  # Fall back to manual construction

        return (
            '<?xml version="1.0"?>'
            f'<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
            f'entityID="{self.sp_entity_id}">'
            f'<md:SPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol" '
            f'AuthnRequestsSigned="false" WantAssertionsSigned="true">'
            f'<md:NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress</md:NameIDFormat>'
            f'<md:AssertionConsumerService '
            f'Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
            f'Location="{self.sp_acs_url}" index="0" isDefault="true"/>'
            f"</md:SPSSODescriptor>"
            f"</md:EntityDescriptor>"
        )
