"""Tools available to the Credentialing Agent."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.engine.tool_executor import ToolDefinition

logger = logging.getLogger(__name__)

# NPPES API configuration
NPPES_API_URL = "https://npiregistry.cms.hhs.gov/api/"
NPPES_TIMEOUT = 10.0
NPPES_MAX_RETRIES = 3


# ── Required documents by credentialing type ─────────────────────────

REQUIRED_DOCUMENTS = {
    "initial": [
        "medical_license",
        "dea_certificate",
        "board_certification",
        "malpractice_insurance",
        "cv_resume",
    ],
    "renewal": [
        "medical_license",
        "dea_certificate",
        "board_certification",
        "malpractice_insurance",
    ],
    "hospital_privileges": [
        "medical_license",
        "dea_certificate",
        "board_certification",
        "malpractice_insurance",
        "cv_resume",
    ],
}


def _nppes_fallback(npi: str) -> dict[str, Any]:
    """Return deterministic mock data when NPPES API is unreachable.

    Used as fallback for development/testing or when the public API is down.
    """
    return {
        "success": True,
        "npi": npi,
        "provider_type": "Individual",
        "name_prefix": "Dr.",
        "first_name": "John",
        "last_name": "Smith",
        "credential": "MD",
        "gender": "M",
        "sole_proprietor": "NO",
        "enumeration_date": "2010-05-15",
        "last_updated": "2024-01-10",
        "taxonomy": {
            "code": "207R00000X",
            "description": "Internal Medicine",
            "primary": True,
            "state": "CA",
            "license": f"A{npi[-6:]}",
        },
        "addresses": [
            {
                "type": "mailing",
                "line1": "123 Medical Plaza",
                "city": "Los Angeles",
                "state": "CA",
                "zip": "90001",
                "phone": "310-555-0100",
            }
        ],
        "_source": "fallback",
    }


def _parse_nppes_response(data: dict[str, Any], npi: str) -> dict[str, Any]:
    """Parse the NPPES API JSON response into our standard format."""
    results = data.get("results", [])
    if not results:
        return {
            "success": False,
            "error": f"NPI '{npi}' not found in NPPES registry.",
        }

    provider = results[0]
    basic = provider.get("basic", {})

    # Extract primary taxonomy
    taxonomies = provider.get("taxonomies", [])
    primary_tax = next(
        (t for t in taxonomies if t.get("primary", False)),
        taxonomies[0] if taxonomies else {},
    )

    # Extract addresses
    addresses_raw = provider.get("addresses", [])
    addresses = []
    for addr in addresses_raw:
        addresses.append({
            "type": addr.get("address_purpose", "").lower().replace("location", "practice").replace("mailing", "mailing"),
            "line1": addr.get("address_1", ""),
            "line2": addr.get("address_2", ""),
            "city": addr.get("city", ""),
            "state": addr.get("state", ""),
            "zip": addr.get("postal_code", ""),
            "phone": addr.get("telephone_number", ""),
        })

    return {
        "success": True,
        "npi": npi,
        "provider_type": "Individual" if provider.get("enumeration_type") == "NPI-1" else "Organization",
        "name_prefix": basic.get("name_prefix", ""),
        "first_name": basic.get("first_name", basic.get("authorized_official_first_name", "")),
        "last_name": basic.get("last_name", basic.get("organization_name", "")),
        "credential": basic.get("credential", ""),
        "gender": basic.get("gender", ""),
        "sole_proprietor": basic.get("sole_proprietor", ""),
        "enumeration_date": basic.get("enumeration_date", ""),
        "last_updated": basic.get("last_updated", ""),
        "taxonomy": {
            "code": primary_tax.get("code", ""),
            "description": primary_tax.get("desc", ""),
            "primary": primary_tax.get("primary", False),
            "state": primary_tax.get("state", ""),
            "license": primary_tax.get("license", ""),
        },
        "addresses": addresses,
        "_source": "nppes_api",
    }


async def lookup_nppes(npi: str) -> dict[str, Any]:
    """Look up a provider in the NPPES NPI Registry.

    Calls the public NPPES API at https://npiregistry.cms.hhs.gov/api/
    with retry logic and exponential backoff with jitter (using the shared
    ``RetryWithBackoff`` utility).  Falls back to deterministic mock data
    if the API is unreachable (e.g., in CI/test environments or during
    outages).
    """
    if not npi or len(npi) != 10 or not npi.isdigit():
        return {
            "success": False,
            "error": f"Invalid NPI format: '{npi}'. Must be 10 digits.",
        }

    from app.core.resilience import RetryWithBackoff

    retry = RetryWithBackoff(
        max_retries=NPPES_MAX_RETRIES - 1,  # RetryWithBackoff adds 1 for initial attempt
        base_delay=1.0,
        max_delay=10.0,
        retryable_exceptions=(
            httpx.HTTPStatusError,
            httpx.RequestError,
            httpx.TimeoutException,
        ),
    )

    async def _do_lookup() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=NPPES_TIMEOUT) as client:
            response = await client.get(
                NPPES_API_URL,
                params={
                    "version": "2.1",
                    "number": npi,
                },
            )
            response.raise_for_status()
            data = response.json()
            result_count = data.get("result_count", 0)
            if result_count == 0:
                return {
                    "success": False,
                    "error": f"NPI '{npi}' not found in NPPES registry.",
                    "_source": "nppes_api",
                }
            return _parse_nppes_response(data, npi)

    try:
        return await retry.execute(_do_lookup)
    except (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException) as exc:
        logger.info(
            "NPPES API unreachable after %d attempts for NPI %s, using fallback data. Last error: %s",
            NPPES_MAX_RETRIES, npi, exc,
        )
        return _nppes_fallback(npi)
    except Exception as exc:
        logger.warning(
            "Unexpected error calling NPPES API for NPI %s: %s", npi, exc,
        )
        return _nppes_fallback(npi)


# ── CAQH mock profiles ───────────────────────────────────────────────
# NPIs ending in an even digit get a "complete" profile (all docs on file);
# NPIs ending in an odd digit get an "incomplete" profile (2 docs missing).
# This allows both the submit and the missing-docs/HITL paths to be
# exercised end-to-end depending on the NPI used in the test.

_CAQH_COMPLETE_DOCS = [
    "medical_license",
    "dea_certificate",
    "board_certification",
    "malpractice_insurance",
    "cv_resume",
]

_CAQH_INCOMPLETE_DOCS = [
    "medical_license",
    "dea_certificate",
    "malpractice_insurance",
]


async def query_caqh(npi: str) -> dict[str, Any]:
    """Retrieve provider data from CAQH ProView.

    Mock implementation — in production, this integrates with
    CAQH's DirectAssure or ProView API.

    The mock uses the last digit of the NPI to select a profile:
    - Even last digit → complete (all documents on file)
    - Odd last digit  → incomplete (missing board_certification, cv_resume)
    """
    if not npi or len(npi) != 10:
        return {"success": False, "error": "Invalid NPI"}

    # Select profile based on last digit parity
    is_complete = int(npi[-1]) % 2 == 0
    documents_on_file = list(_CAQH_COMPLETE_DOCS if is_complete else _CAQH_INCOMPLETE_DOCS)
    missing = [
        doc for doc in _CAQH_COMPLETE_DOCS if doc not in documents_on_file
    ]

    return {
        "success": True,
        "npi": npi,
        "caqh_id": f"CAQH-{npi[-6:]}",
        "attestation_status": "current",
        "last_attestation_date": "2025-11-01",
        "documents_on_file": documents_on_file,
        "missing_documents": missing,
        "education": [
            {
                "type": "medical_school",
                "institution": "UCLA School of Medicine",
                "graduation_year": "2008",
            }
        ],
        "training": [
            {
                "type": "residency",
                "specialty": "Internal Medicine",
                "institution": "Cedars-Sinai Medical Center",
                "completion_year": "2011",
            }
        ],
    }


async def verify_state_license(
    npi: str, state: str, license_number: str = ""
) -> dict[str, Any]:
    """Verify a provider's state medical license.

    Mock implementation — in production, queries state licensing
    board APIs or databases.
    """
    if not state or len(state) != 2:
        return {"success": False, "error": "Invalid state code"}

    return {
        "success": True,
        "npi": npi,
        "state": state,
        "license_number": license_number or f"{state}-{npi[-6:]}",
        "license_status": "active",
        "issue_date": "2011-07-01",
        "expiration_date": "2027-06-30",
        "discipline_actions": [],
        "verified": True,
    }


class OIGExclusionProvider:
    """Abstract interface for OIG/SAM exclusion list lookups.

    Subclass and override ``check`` to integrate with the real OIG LEIE
    API (https://oig.hhs.gov/exclusions/exclusions_list.asp) or the
    SAM.gov Entity Management API.
    """

    async def check(self, npi: str, provider_name: str = "") -> dict[str, Any]:
        """Return exclusion check results.

        Must return a dict with at least:
        - ``success``: bool
        - ``oig_excluded``: bool
        - ``sam_excluded``: bool
        """
        raise NotImplementedError(
            "OIGExclusionProvider.check() must be overridden by a concrete implementation"
        )


class MockOIGExclusionProvider(OIGExclusionProvider):
    """Deterministic mock — always returns clean results.

    Used in development/testing environments.
    """

    async def check(self, npi: str, provider_name: str = "") -> dict[str, Any]:
        from datetime import date as _date

        today = _date.today().isoformat()
        return {
            "success": True,
            "npi": npi,
            "provider_name": provider_name,
            "oig_excluded": False,
            "sam_excluded": False,
            "oig_check_date": today,
            "sam_check_date": today,
            "exclusion_details": None,
        }


class ExcludedOIGProvider(OIGExclusionProvider):
    """Test provider that always returns an exclusion hit.

    Used in tests to exercise the denial/escalation path.
    """

    async def check(self, npi: str, provider_name: str = "") -> dict[str, Any]:
        from datetime import date as _date

        today = _date.today().isoformat()
        return {
            "success": True,
            "npi": npi,
            "provider_name": provider_name,
            "oig_excluded": True,
            "sam_excluded": False,
            "oig_check_date": today,
            "sam_check_date": today,
            "exclusion_details": {
                "exclusion_type": "1128(a)(1)",
                "exclusion_date": "2024-01-15",
                "reason": "Program-related conviction",
            },
        }


class HttpOIGExclusionProvider(OIGExclusionProvider):
    """Production provider that queries the OIG LEIE API and SAM.gov.

    Uses the OIG LEIE exclusion verification endpoint and the SAM.gov
    Entity Management API to check whether a provider is excluded from
    federal healthcare programmes.

    Requires ``SLATE_OIG_API_BASE_URL`` to be set (e.g.
    ``https://oig.hhs.gov/exclusions/``).
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    async def check(self, npi: str, provider_name: str = "") -> dict[str, Any]:
        import httpx
        from datetime import date as _date
        from app.core.resilience import resilient_http_get

        today = _date.today().isoformat()
        oig_excluded = False
        sam_excluded = False
        exclusion_details = None

        # 1. Query OIG LEIE (with retry)
        try:
            oig_url = f"{self._base_url}/exclusions/search"
            oig_resp = await resilient_http_get(
                oig_url,
                timeout=30.0,
                params={"npi": npi, "name": provider_name},
            )
            oig_resp.raise_for_status()
            oig_data = oig_resp.json()

            if oig_data.get("total", 0) > 0:
                oig_excluded = True
                first_hit = oig_data.get("results", [{}])[0]
                exclusion_details = {
                    "exclusion_type": first_hit.get("excltype", ""),
                    "exclusion_date": first_hit.get("excldate", ""),
                    "reason": first_hit.get("general", ""),
                }
        except httpx.HTTPError as exc:
            logger.warning("OIG LEIE API request failed for NPI %s: %s", npi, exc)
            return {
                "success": False,
                "npi": npi,
                "provider_name": provider_name,
                "error": f"OIG LEIE API request failed: {exc}",
                "oig_excluded": False,
                "sam_excluded": False,
                "oig_check_date": today,
                "sam_check_date": None,
            }

        # 2. Query SAM.gov exclusions (with retry)
        try:
            sam_url = f"{self._base_url}/sam/exclusions"
            sam_resp = await resilient_http_get(
                sam_url,
                timeout=30.0,
                params={"npi": npi},
            )
            sam_resp.raise_for_status()
            sam_data = sam_resp.json()
            if sam_data.get("totalRecords", 0) > 0:
                sam_excluded = True
        except httpx.HTTPError as exc:
            logger.warning("SAM.gov API request failed for NPI %s: %s", npi, exc)
            # OIG check succeeded; report SAM as unknown rather than failing
            return {
                "success": True,
                "npi": npi,
                "provider_name": provider_name,
                "oig_excluded": oig_excluded,
                "sam_excluded": False,
                "oig_check_date": today,
                "sam_check_date": None,
                "exclusion_details": exclusion_details,
                "_warning": f"SAM.gov check failed: {exc}",
            }

        return {
            "success": True,
            "npi": npi,
            "provider_name": provider_name,
            "oig_excluded": oig_excluded,
            "sam_excluded": sam_excluded,
            "oig_check_date": today,
            "sam_check_date": today,
            "exclusion_details": exclusion_details,
        }


def _create_default_oig_provider() -> OIGExclusionProvider:
    """Select the OIG provider based on environment configuration.

    Returns ``HttpOIGExclusionProvider`` when ``SLATE_OIG_API_BASE_URL``
    is configured, otherwise falls back to ``MockOIGExclusionProvider``.
    """
    try:
        from app.config import settings

        if settings.oig_api_base_url:
            logger.info(
                "Using HttpOIGExclusionProvider with base URL: %s",
                settings.oig_api_base_url,
            )
            return HttpOIGExclusionProvider(base_url=settings.oig_api_base_url)
    except Exception as exc:
        logger.warning("Failed to load OIG config, using mock provider: %s", exc)

    logger.info("Using MockOIGExclusionProvider (SLATE_OIG_API_BASE_URL not set)")
    return MockOIGExclusionProvider()


# Module-level provider — swap via ``set_oig_provider()`` for testing or
# production wiring.  Auto-selects based on configuration.
_oig_provider: OIGExclusionProvider = _create_default_oig_provider()


def set_oig_provider(provider: OIGExclusionProvider) -> None:
    """Replace the module-level OIG exclusion provider."""
    global _oig_provider
    _oig_provider = provider


def get_oig_provider() -> OIGExclusionProvider:
    """Return the current OIG exclusion provider."""
    return _oig_provider


async def check_oig_exclusion(npi: str, provider_name: str = "") -> dict[str, Any]:
    """Check the OIG List of Excluded Individuals/Entities (LEIE).

    Also checks SAM.gov for debarment.  Delegates to the configured
    ``OIGExclusionProvider`` (defaults to ``MockOIGExclusionProvider``
    in dev/test; production should wire a real provider).
    """
    if not npi:
        return {"success": False, "error": "NPI is required"}

    return await _oig_provider.check(npi, provider_name)


async def compile_application(
    npi: str,
    provider_details: dict[str, Any],
    verification_results: dict[str, Any],
    documents_checklist: dict[str, Any],
    target_organization: str = "",
    target_payer_id: str = "",
) -> dict[str, Any]:
    """Compile a credentialing application from verified data.

    Assembles all gathered information into a structured
    application ready for submission.
    """
    missing = documents_checklist.get("missing", [])
    is_complete = len(missing) == 0

    application = {
        "npi": npi,
        "provider_name": f"{provider_details.get('first_name', '')} {provider_details.get('last_name', '')}".strip(),
        "credential": provider_details.get("credential", ""),
        "specialty": provider_details.get("taxonomy", {}).get("description", ""),
        "target_organization": target_organization,
        "target_payer_id": target_payer_id,
        "licenses": verification_results.get("licenses", []),
        "sanctions_clear": verification_results.get("sanctions_clear", True),
        "documents_complete": is_complete,
        "missing_documents": missing,
        "education": provider_details.get("education", []),
        "training": provider_details.get("training", []),
        "ready_to_submit": is_complete and verification_results.get("sanctions_clear", True),
    }

    return {
        "success": True,
        "application": application,
        "ready_to_submit": application["ready_to_submit"],
        "missing_documents": missing,
    }


async def submit_application(
    application_data: dict[str, Any],
    target_organization: str = "",
    target_payer_id: str = "",
) -> dict[str, Any]:
    """Submit the credentialing application to the target org/payer.

    Mock implementation — returns a tracking number and pending status.
    """
    if not application_data.get("ready_to_submit", False):
        return {
            "success": False,
            "error": "Application is not ready for submission. Missing documents must be resolved first.",
            "missing_documents": application_data.get("missing_documents", []),
        }

    from datetime import date as _date

    today = _date.today()
    return {
        "success": True,
        "tracking_number": f"CRED-{application_data.get('npi', 'UNKNOWN')[-6:]}-{today.year}",
        "status": "submitted",
        "estimated_review_days": 90,
        "submission_date": today.isoformat(),
    }


def get_credentialing_tools() -> list[ToolDefinition]:
    """Return all tool definitions for the credentialing agent."""
    return [
        ToolDefinition(
            name="lookup_nppes",
            description="Look up provider details in the NPPES NPI Registry",
            parameters={
                "npi": {"type": "string", "description": "10-digit NPI number"},
            },
            required_params=["npi"],
            handler=lookup_nppes,
        ),
        ToolDefinition(
            name="query_caqh",
            description="Retrieve provider credentialing data from CAQH ProView",
            parameters={
                "npi": {"type": "string", "description": "10-digit NPI number"},
            },
            required_params=["npi"],
            handler=query_caqh,
        ),
        ToolDefinition(
            name="verify_state_license",
            description="Verify a provider's state medical license status",
            parameters={
                "npi": {"type": "string", "description": "Provider NPI"},
                "state": {"type": "string", "description": "Two-letter state code"},
                "license_number": {"type": "string", "description": "License number"},
            },
            required_params=["npi", "state"],
            handler=verify_state_license,
        ),
        ToolDefinition(
            name="check_oig_exclusion",
            description="Check OIG exclusion list and SAM.gov for sanctions",
            parameters={
                "npi": {"type": "string", "description": "Provider NPI"},
                "provider_name": {"type": "string", "description": "Provider name"},
            },
            required_params=["npi"],
            handler=check_oig_exclusion,
        ),
        ToolDefinition(
            name="compile_application",
            description="Compile verified data into a credentialing application",
            parameters={
                "npi": {"type": "string"},
                "provider_details": {"type": "object"},
                "verification_results": {"type": "object"},
                "documents_checklist": {"type": "object"},
                "target_organization": {"type": "string"},
                "target_payer_id": {"type": "string"},
            },
            required_params=["npi", "provider_details", "verification_results", "documents_checklist"],
            handler=compile_application,
        ),
        ToolDefinition(
            name="submit_application",
            description="Submit compiled credentialing application to target organization/payer",
            parameters={
                "application_data": {"type": "object"},
                "target_organization": {"type": "string"},
                "target_payer_id": {"type": "string"},
            },
            required_params=["application_data"],
            handler=submit_application,
        ),
    ]
