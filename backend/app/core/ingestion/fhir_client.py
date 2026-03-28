"""FHIR R4 async HTTP client for Patient, Encounter, Condition, Appointment, Coverage resources.

Provides retry logic with exponential backoff and structured response parsing.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Default retry configuration
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_FACTOR = 0.5
DEFAULT_TIMEOUT = 30.0

# Supported FHIR resource types
SUPPORTED_RESOURCES = frozenset(
    {"Patient", "Encounter", "Condition", "Appointment", "Coverage", "Slot",
     "MedicationRequest", "Observation", "Procedure", "DocumentReference"}
)


class FHIRClientError(Exception):
    """Base exception for FHIR client errors."""

    def __init__(self, message: str, status_code: int | None = None, response_body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class FHIRResourceNotFound(FHIRClientError):
    """Raised when a requested FHIR resource is not found."""
    pass


class FHIRClient:
    """Async FHIR R4 client with retry logic.

    Args:
        base_url: FHIR server base URL (e.g. https://fhir.example.com/r4).
        auth_token: Optional Bearer token for authentication.
        max_retries: Maximum number of retry attempts for transient errors.
        timeout: Request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        auth_token: str | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.timeout = timeout

        headers: dict[str, str] = {
            "Accept": "application/fhir+json",
            "Content-Type": "application/fhir+json",
        }
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> FHIRClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # HTTP status codes that are considered transient and should be retried
    RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute an HTTP request with retry logic for transient errors.

        Retries on connection errors, timeouts, and transient HTTP status codes
        (429, 500, 502, 503, 504) with capped exponential backoff.
        """
        import asyncio

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                )
                if response.status_code == 404:
                    raise FHIRResourceNotFound(
                        f"Resource not found: {path}",
                        status_code=404,
                        response_body=response.text,
                    )
                # Retry on transient HTTP errors
                if response.status_code in self.RETRYABLE_STATUS_CODES:
                    last_exc = FHIRClientError(
                        f"FHIR request failed: {response.status_code}",
                        status_code=response.status_code,
                        response_body=response.text,
                    )
                    if attempt < self.max_retries:
                        wait = min(DEFAULT_BACKOFF_FACTOR * (2 ** attempt), 10.0)
                        logger.warning(
                            "FHIR request to %s returned %d (attempt %d/%d), retrying in %.1fs",
                            path, response.status_code, attempt + 1, self.max_retries + 1, wait,
                        )
                        await asyncio.sleep(wait)
                        continue
                    else:
                        raise last_exc
                if response.status_code >= 400:
                    raise FHIRClientError(
                        f"FHIR request failed: {response.status_code}",
                        status_code=response.status_code,
                        response_body=response.text,
                    )
                return response.json()
            except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadTimeout) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    wait = min(DEFAULT_BACKOFF_FACTOR * (2 ** attempt), 10.0)
                    logger.warning(
                        "FHIR request to %s failed (attempt %d/%d), retrying in %.1fs: %s",
                        path, attempt + 1, self.max_retries + 1, wait, exc,
                    )
                    await asyncio.sleep(wait)
                else:
                    raise FHIRClientError(
                        f"FHIR request failed after {self.max_retries + 1} attempts: {exc}"
                    ) from last_exc
        # Should not reach here, but satisfy type checker
        raise FHIRClientError("Request failed")  # pragma: no cover

    # ── Resource Read Operations ───────────────────────────────────────

    async def read(self, resource_type: str, resource_id: str) -> dict[str, Any]:
        """Read a single FHIR resource by type and ID.

        Args:
            resource_type: FHIR resource type (e.g. 'Patient', 'Coverage').
            resource_id: Logical resource ID.

        Returns:
            Parsed FHIR resource as a dictionary.
        """
        if resource_type not in SUPPORTED_RESOURCES:
            raise ValueError(f"Unsupported resource type: {resource_type}")
        return await self._request("GET", f"/{resource_type}/{resource_id}")

    async def search(
        self,
        resource_type: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Search for FHIR resources.

        Args:
            resource_type: FHIR resource type.
            params: Search parameters.

        Returns:
            FHIR Bundle containing matching resources.
        """
        if resource_type not in SUPPORTED_RESOURCES:
            raise ValueError(f"Unsupported resource type: {resource_type}")
        return await self._request("GET", f"/{resource_type}", params=params)

    async def create(self, resource_type: str, resource: dict[str, Any]) -> dict[str, Any]:
        """Create a new FHIR resource.

        Args:
            resource_type: FHIR resource type.
            resource: Resource body.

        Returns:
            Created resource with server-assigned ID.
        """
        if resource_type not in SUPPORTED_RESOURCES:
            raise ValueError(f"Unsupported resource type: {resource_type}")
        return await self._request("POST", f"/{resource_type}", json_body=resource)

    # ── Convenience Methods ────────────────────────────────────────────

    async def get_patient(self, patient_id: str) -> dict[str, Any]:
        """Fetch a Patient resource by ID."""
        return await self.read("Patient", patient_id)

    async def search_patients(self, **params: Any) -> list[dict[str, Any]]:
        """Search for Patient resources. Returns list of entries."""
        bundle = await self.search("Patient", params=params)
        return _extract_entries(bundle)

    async def get_coverage(self, coverage_id: str) -> dict[str, Any]:
        """Fetch a Coverage resource by ID."""
        return await self.read("Coverage", coverage_id)

    async def search_coverage(self, **params: Any) -> list[dict[str, Any]]:
        """Search for Coverage resources by patient or other criteria."""
        bundle = await self.search("Coverage", params=params)
        return _extract_entries(bundle)

    async def get_encounter(self, encounter_id: str) -> dict[str, Any]:
        """Fetch an Encounter resource by ID."""
        return await self.read("Encounter", encounter_id)

    async def get_appointment(self, appointment_id: str) -> dict[str, Any]:
        """Fetch an Appointment resource by ID."""
        return await self.read("Appointment", appointment_id)

    async def search_appointments(self, **params: Any) -> list[dict[str, Any]]:
        """Search for Appointment resources."""
        bundle = await self.search("Appointment", params=params)
        return _extract_entries(bundle)

    async def search_slots(self, **params: Any) -> list[dict[str, Any]]:
        """Search for available Slot resources."""
        bundle = await self.search("Slot", params=params)
        return _extract_entries(bundle)

    async def get_condition(self, condition_id: str) -> dict[str, Any]:
        """Fetch a Condition resource by ID."""
        return await self.read("Condition", condition_id)

    async def search_conditions(self, **params: Any) -> list[dict[str, Any]]:
        """Search for Condition resources (e.g. by patient)."""
        bundle = await self.search("Condition", params=params)
        return _extract_entries(bundle)

    async def search_observations(self, **params: Any) -> list[dict[str, Any]]:
        """Search for Observation resources (lab results, vitals)."""
        bundle = await self.search("Observation", params=params)
        return _extract_entries(bundle)

    async def search_medication_requests(self, **params: Any) -> list[dict[str, Any]]:
        """Search for MedicationRequest resources."""
        bundle = await self.search("MedicationRequest", params=params)
        return _extract_entries(bundle)


def _extract_entries(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract resource entries from a FHIR Bundle response."""
    entries = bundle.get("entry", [])
    return [entry.get("resource", entry) for entry in entries]
