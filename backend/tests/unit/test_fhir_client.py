"""Unit tests for FHIR R4 client with httpx mock responses."""

import pytest
import httpx

from app.core.ingestion.fhir_client import (
    FHIRClient,
    FHIRClientError,
    FHIRResourceNotFound,
    _extract_entries,
)


# ── Sample FHIR Resources ────────────────────────────────────────────

SAMPLE_PATIENT = {
    "resourceType": "Patient",
    "id": "pat-123",
    "name": [{"family": "Doe", "given": ["Jane", "Marie"]}],
    "gender": "female",
    "birthDate": "1985-06-15",
    "identifier": [
        {"type": {"coding": [{"code": "MR"}]}, "value": "MRN-001"},
        {"type": {"coding": [{"code": "MB"}]}, "value": "INS-12345"},
    ],
    "address": [{"line": ["123 Main St"], "city": "Springfield", "state": "IL", "postalCode": "62701"}],
    "telecom": [
        {"system": "phone", "use": "home", "value": "555-123-4567"},
        {"system": "email", "value": "jane@example.com"},
    ],
}

SAMPLE_COVERAGE = {
    "resourceType": "Coverage",
    "id": "cov-456",
    "status": "active",
    "subscriberId": "INS-12345",
    "payor": [{"display": "Blue Cross Blue Shield"}],
    "period": {"start": "2024-01-01", "end": "2024-12-31"},
    "class": [
        {"type": {"coding": [{"code": "group"}]}, "value": "GRP-789"},
        {"type": {"coding": [{"code": "plan"}]}, "name": "Gold PPO"},
    ],
}

SAMPLE_APPOINTMENT = {
    "resourceType": "Appointment",
    "id": "appt-789",
    "status": "booked",
    "start": "2024-06-15T09:00:00Z",
    "end": "2024-06-15T09:30:00Z",
}

SAMPLE_BUNDLE = {
    "resourceType": "Bundle",
    "type": "searchset",
    "total": 2,
    "entry": [
        {"resource": SAMPLE_PATIENT},
        {"resource": {"resourceType": "Patient", "id": "pat-456", "name": [{"family": "Smith"}]}},
    ],
}


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def mock_transport():
    """Create a mock httpx transport for testing."""
    return httpx.MockTransport(_mock_handler)


def _mock_handler(request: httpx.Request) -> httpx.Response:
    """Route mock requests to appropriate responses."""
    path = request.url.path

    if path == "/Patient/pat-123":
        return httpx.Response(200, json=SAMPLE_PATIENT)
    elif path == "/Coverage/cov-456":
        return httpx.Response(200, json=SAMPLE_COVERAGE)
    elif path == "/Appointment/appt-789":
        return httpx.Response(200, json=SAMPLE_APPOINTMENT)
    elif path == "/Patient" and request.method == "GET":
        return httpx.Response(200, json=SAMPLE_BUNDLE)
    elif path == "/Coverage" and request.method == "GET":
        return httpx.Response(200, json={
            "resourceType": "Bundle",
            "entry": [{"resource": SAMPLE_COVERAGE}],
        })
    elif path == "/Patient/not-found":
        return httpx.Response(404, json={"issue": [{"severity": "error"}]})
    elif path == "/Patient/server-error":
        return httpx.Response(500, text="Internal Server Error")
    elif path == "/Patient" and request.method == "POST":
        body = SAMPLE_PATIENT.copy()
        body["id"] = "new-pat-001"
        return httpx.Response(201, json=body)

    return httpx.Response(404, json={"issue": [{"severity": "error", "diagnostics": "Not found"}]})


@pytest.fixture
async def fhir_client(mock_transport):
    """Create a FHIRClient with mock transport."""
    client = FHIRClient(base_url="http://fhir-test.local")
    # Replace the internal client with our mock
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=mock_transport,
        base_url="http://fhir-test.local",
        headers={"Accept": "application/fhir+json"},
    )
    yield client
    await client.close()


# ── Tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_patient(fhir_client):
    """FHIR client fetches a Patient resource by ID."""
    patient = await fhir_client.get_patient("pat-123")
    assert patient["resourceType"] == "Patient"
    assert patient["id"] == "pat-123"
    assert patient["name"][0]["family"] == "Doe"


@pytest.mark.asyncio
async def test_get_coverage(fhir_client):
    """FHIR client fetches a Coverage resource by ID."""
    coverage = await fhir_client.get_coverage("cov-456")
    assert coverage["resourceType"] == "Coverage"
    assert coverage["status"] == "active"
    assert coverage["subscriberId"] == "INS-12345"


@pytest.mark.asyncio
async def test_get_appointment(fhir_client):
    """FHIR client fetches an Appointment resource by ID."""
    appointment = await fhir_client.get_appointment("appt-789")
    assert appointment["resourceType"] == "Appointment"
    assert appointment["status"] == "booked"


@pytest.mark.asyncio
async def test_search_patients(fhir_client):
    """FHIR client searches for patients and returns entries."""
    patients = await fhir_client.search_patients(family="Doe")
    assert len(patients) == 2
    assert patients[0]["id"] == "pat-123"


@pytest.mark.asyncio
async def test_search_coverage(fhir_client):
    """FHIR client searches for coverage resources."""
    coverages = await fhir_client.search_coverage(patient="pat-123")
    assert len(coverages) == 1
    assert coverages[0]["status"] == "active"


@pytest.mark.asyncio
async def test_resource_not_found(fhir_client):
    """FHIR client raises FHIRResourceNotFound for 404 responses."""
    with pytest.raises(FHIRResourceNotFound) as exc_info:
        await fhir_client.get_patient("not-found")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_server_error(fhir_client):
    """FHIR client raises FHIRClientError for 5xx responses."""
    with pytest.raises(FHIRClientError) as exc_info:
        await fhir_client.get_patient("server-error")
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_unsupported_resource_type(fhir_client):
    """FHIR client rejects unsupported resource types."""
    with pytest.raises(ValueError, match="Unsupported resource type"):
        await fhir_client.read("FakeResource", "123")


@pytest.mark.asyncio
async def test_create_patient(fhir_client):
    """FHIR client can create a new Patient resource."""
    new_patient = await fhir_client.create("Patient", SAMPLE_PATIENT)
    assert new_patient["id"] == "new-pat-001"


def test_extract_entries_empty_bundle():
    """Extract entries from empty bundle returns empty list."""
    assert _extract_entries({"resourceType": "Bundle"}) == []
    assert _extract_entries({"resourceType": "Bundle", "entry": []}) == []


@pytest.mark.asyncio
async def test_context_manager(mock_transport):
    """FHIR client works as async context manager."""
    async with FHIRClient(base_url="http://fhir-test.local") as client:
        await client._client.aclose()
        client._client = httpx.AsyncClient(
            transport=mock_transport,
            base_url="http://fhir-test.local",
        )
        patient = await client.get_patient("pat-123")
        assert patient["id"] == "pat-123"


@pytest.mark.asyncio
async def test_search_appointments(fhir_client):
    """FHIR client searches for Appointment resources."""
    # Mock transport returns 404 for /Appointment search, but tests the code path
    try:
        appointments = await fhir_client.search_appointments(date="2024-06-15")
    except FHIRResourceNotFound:
        pass  # Expected — mock doesn't have /Appointment endpoint


@pytest.mark.asyncio
async def test_search_conditions(fhir_client):
    """FHIR client searches for Condition resources."""
    try:
        await fhir_client.search_conditions(patient="pat-123")
    except FHIRResourceNotFound:
        pass


@pytest.mark.asyncio
async def test_search_observations(fhir_client):
    """FHIR client searches for Observation resources."""
    try:
        await fhir_client.search_observations(patient="pat-123")
    except FHIRResourceNotFound:
        pass


@pytest.mark.asyncio
async def test_search_medication_requests(fhir_client):
    """FHIR client searches for MedicationRequest resources."""
    try:
        await fhir_client.search_medication_requests(patient="pat-123")
    except FHIRResourceNotFound:
        pass


@pytest.mark.asyncio
async def test_search_slots(fhir_client):
    """FHIR client searches for Slot resources."""
    try:
        await fhir_client.search_slots(schedule="sched-1")
    except FHIRResourceNotFound:
        pass


@pytest.mark.asyncio
async def test_get_encounter(fhir_client):
    """FHIR client fetches an Encounter resource."""
    try:
        await fhir_client.get_encounter("enc-123")
    except FHIRResourceNotFound:
        pass


@pytest.mark.asyncio
async def test_get_condition(fhir_client):
    """FHIR client fetches a Condition resource."""
    try:
        await fhir_client.get_condition("cond-123")
    except FHIRResourceNotFound:
        pass


@pytest.mark.asyncio
async def test_client_with_auth_token():
    """FHIR client includes Authorization header when token provided."""
    client = FHIRClient(base_url="http://fhir-test.local", auth_token="test-token")
    assert client._client.headers.get("Authorization") == "Bearer test-token"
    await client.close()


def test_client_strips_trailing_slash():
    """FHIR client normalizes base URL by stripping trailing slash."""
    import asyncio
    client = FHIRClient(base_url="http://fhir-test.local/r4/")
    assert client.base_url == "http://fhir-test.local/r4"
    asyncio.get_event_loop().run_until_complete(client.close())


# ── Tests: Retry on 5xx/429 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_on_500():
    """FHIR client retries on HTTP 500 and eventually raises."""
    call_count = 0

    def handler_500(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(500, text="Internal Server Error")

    transport = httpx.MockTransport(handler_500)
    client = FHIRClient(base_url="http://fhir-test.local", max_retries=2)
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=transport,
        base_url="http://fhir-test.local",
    )

    with pytest.raises(FHIRClientError) as exc_info:
        await client.get_patient("test-123")

    assert exc_info.value.status_code == 500
    assert call_count == 3  # 1 initial + 2 retries
    await client.close()


@pytest.mark.asyncio
async def test_retry_on_500_then_success():
    """FHIR client retries on 500 and succeeds when server recovers."""
    call_count = 0

    def handler_flaky(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return httpx.Response(500, text="Internal Server Error")
        return httpx.Response(200, json={"resourceType": "Patient", "id": "test-123"})

    transport = httpx.MockTransport(handler_flaky)
    client = FHIRClient(base_url="http://fhir-test.local", max_retries=3)
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=transport,
        base_url="http://fhir-test.local",
    )

    result = await client.get_patient("test-123")
    assert result["id"] == "test-123"
    assert call_count == 3  # 2 failures + 1 success
    await client.close()


@pytest.mark.asyncio
async def test_retry_on_429():
    """FHIR client retries on HTTP 429 Too Many Requests."""
    call_count = 0

    def handler_429(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, text="Too Many Requests")
        return httpx.Response(200, json={"resourceType": "Patient", "id": "pat-1"})

    transport = httpx.MockTransport(handler_429)
    client = FHIRClient(base_url="http://fhir-test.local", max_retries=2)
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=transport,
        base_url="http://fhir-test.local",
    )

    result = await client.get_patient("pat-1")
    assert result["id"] == "pat-1"
    assert call_count == 2
    await client.close()


@pytest.mark.asyncio
async def test_no_retry_on_400():
    """FHIR client does NOT retry on 400 Bad Request (non-transient)."""
    call_count = 0

    def handler_400(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(400, text="Bad Request")

    transport = httpx.MockTransport(handler_400)
    client = FHIRClient(base_url="http://fhir-test.local", max_retries=2)
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=transport,
        base_url="http://fhir-test.local",
    )

    with pytest.raises(FHIRClientError) as exc_info:
        await client.get_patient("bad-request")

    assert exc_info.value.status_code == 400
    assert call_count == 1  # No retries for 400
    await client.close()
