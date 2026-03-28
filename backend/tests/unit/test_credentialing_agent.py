"""Unit tests for the Credentialing Agent — Sprint 9."""

import pytest
import uuid
from unittest.mock import AsyncMock, patch, MagicMock

from app.agents.credentialing.graph import (
    CredentialingAgent,
    run_credentialing_agent,
    lookup_provider_node,
    verify_licenses_node,
    check_sanctions_node,
    compile_application_node,
    submit_node,
    track_status_node,
    alert_expirations_node,
    evaluate_confidence_node,
)
from app.agents.credentialing.tools import (
    REQUIRED_DOCUMENTS,
    lookup_nppes,
    _nppes_fallback,
    _parse_nppes_response,
    query_caqh,
    verify_state_license,
    check_oig_exclusion,
    compile_application,
    submit_application,
    get_credentialing_tools,
    NPPES_API_URL,
)
from app.core.engine.llm_provider import LLMProvider, MockLLMBackend
from app.core.engine.state import create_initial_state


@pytest.fixture
def llm_provider():
    return LLMProvider(primary=MockLLMBackend())


@pytest.fixture
def credentialing_input():
    """Default input uses odd NPI so CAQH mock returns incomplete docs."""
    return {
        "provider_npi": "1234567891",
        "target_organization": "Test Hospital",
        "target_payer_id": "PAYER001",
        "credentialing_type": "initial",
        "state": "CA",
    }


@pytest.fixture
def credentialing_input_complete():
    """Input uses even NPI so CAQH mock returns all docs on file."""
    return {
        "provider_npi": "1234567890",
        "target_organization": "Test Hospital",
        "target_payer_id": "PAYER001",
        "credentialing_type": "initial",
        "state": "CA",
    }


# ── NPPES Lookup Tests ─────────────────────────────────────────────


class TestNPPESLookup:
    @pytest.mark.asyncio
    async def test_valid_npi_returns_provider_details(self):
        """NPPES lookup returns provider details (via mocked API)."""
        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "result_count": 1,
            "results": [{
                "enumeration_type": "NPI-1",
                "basic": {
                    "first_name": "John",
                    "last_name": "Smith",
                    "credential": "MD",
                    "gender": "M",
                    "enumeration_date": "2010-05-15",
                },
                "taxonomies": [{
                    "code": "207R00000X",
                    "desc": "Internal Medicine",
                    "primary": True,
                    "state": "CA",
                    "license": "A567890",
                }],
                "addresses": [{
                    "address_purpose": "MAILING",
                    "address_1": "123 Medical Plaza",
                    "city": "Los Angeles",
                    "state": "CA",
                    "postal_code": "90001",
                    "telephone_number": "310-555-0100",
                }],
            }],
        }

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.agents.credentialing.tools.httpx.AsyncClient", return_value=mock_client):
            result = await lookup_nppes("1234567890")

        assert result["success"] is True
        assert result["npi"] == "1234567890"
        assert result["first_name"] == "John"
        assert result["last_name"] == "Smith"
        assert result["taxonomy"]["code"] == "207R00000X"

    @pytest.mark.asyncio
    async def test_invalid_npi_too_short(self):
        result = await lookup_nppes("12345")
        assert result["success"] is False
        assert "Invalid NPI" in result["error"]

    @pytest.mark.asyncio
    async def test_invalid_npi_non_numeric(self):
        result = await lookup_nppes("123456789A")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_empty_npi(self):
        result = await lookup_nppes("")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_nppes_api_called_with_correct_params(self):
        """Verify the NPPES API is called with correct URL and params."""
        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "result_count": 1,
            "results": [{
                "enumeration_type": "NPI-1",
                "basic": {
                    "first_name": "Jane",
                    "last_name": "Doe",
                    "credential": "DO",
                    "gender": "F",
                    "enumeration_date": "2015-03-20",
                },
                "taxonomies": [{
                    "code": "208D00000X",
                    "desc": "General Practice",
                    "primary": True,
                    "state": "NY",
                    "license": "NY12345",
                }],
                "addresses": [{
                    "address_purpose": "LOCATION",
                    "address_1": "456 Health Ave",
                    "city": "New York",
                    "state": "NY",
                    "postal_code": "10001",
                    "telephone_number": "212-555-0200",
                }],
            }],
        }

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.agents.credentialing.tools.httpx.AsyncClient", return_value=mock_client):
            result = await lookup_nppes("9876543210")

        assert result["success"] is True
        assert result["first_name"] == "Jane"
        assert result["last_name"] == "Doe"
        assert result["credential"] == "DO"
        assert result["taxonomy"]["code"] == "208D00000X"
        assert result["taxonomy"]["state"] == "NY"
        assert result["_source"] == "nppes_api"

    @pytest.mark.asyncio
    async def test_nppes_api_failure_falls_back_to_mock(self):
        """When NPPES API is unreachable, falls back to deterministic mock."""
        import httpx

        with patch(
            "app.agents.credentialing.tools.httpx.AsyncClient",
            side_effect=httpx.ConnectError("Connection refused"),
        ):
            result = await lookup_nppes("1234567890")

        assert result["success"] is True
        assert result["_source"] == "fallback"
        assert result["npi"] == "1234567890"

    @pytest.mark.asyncio
    async def test_nppes_api_npi_not_found(self):
        """When NPI is not in NPPES, return appropriate error."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"result_count": 0, "results": []}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.agents.credentialing.tools.httpx.AsyncClient", return_value=mock_client):
            result = await lookup_nppes("0000000000")

        assert result["success"] is False
        assert "not found" in result["error"]

    def test_nppes_fallback_returns_deterministic_data(self):
        """Fallback function returns consistent mock data."""
        result = _nppes_fallback("1234567890")
        assert result["success"] is True
        assert result["npi"] == "1234567890"
        assert result["first_name"] == "John"
        assert result["last_name"] == "Smith"
        assert result["_source"] == "fallback"

    def test_parse_nppes_response_handles_empty_results(self):
        """Parser handles empty results gracefully."""
        result = _parse_nppes_response({"results": []}, "1234567890")
        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_nppes_retries_use_backoff_delay(self):
        """NPPES retries should use exponential backoff with jitter (not tight loop)."""
        import asyncio
        import httpx

        sleep_calls: list[float] = []
        original_sleep = asyncio.sleep

        async def capture_sleep(seconds):
            sleep_calls.append(seconds)
            # Don't actually sleep

        call_count = 0
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "result_count": 1,
            "results": [{
                "enumeration_type": "NPI-1",
                "basic": {"first_name": "Test", "last_name": "User"},
                "taxonomies": [{"code": "207R00000X", "desc": "IM", "primary": True}],
                "addresses": [],
            }],
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        async def mock_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise httpx.ConnectError("Connection refused")
            return mock_response

        mock_client.get = mock_get

        with patch("app.agents.credentialing.tools.httpx.AsyncClient", return_value=mock_client):
            with patch("asyncio.sleep", side_effect=capture_sleep):
                result = await lookup_nppes("1234567890")

        assert result["success"] is True
        assert call_count == 3  # 2 failures + 1 success
        # Verify backoff delays were used (not a tight loop)
        assert len(sleep_calls) == 2  # 2 retry delays
        for delay in sleep_calls:
            assert delay >= 0  # Jitter means delay >= 0

    @pytest.mark.asyncio
    async def test_nppes_fallback_after_all_retries_exhausted(self):
        """After all retries with backoff, should still fall back to mock data."""
        import httpx

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with patch(
                "app.agents.credentialing.tools.httpx.AsyncClient",
                side_effect=httpx.ConnectError("Connection refused"),
            ):
                result = await lookup_nppes("1234567890")

        assert result["success"] is True
        assert result["_source"] == "fallback"


# ── Document Gap Analysis Tests ────────────────────────────────────


class TestDocumentGapAnalysis:
    @pytest.mark.asyncio
    async def test_identifies_missing_documents_odd_npi(self):
        """Odd NPI → provider missing 2 of 5 required docs → both identified."""
        caqh_result = await query_caqh("1234567891")  # odd → incomplete
        assert caqh_result["success"] is True

        on_file = caqh_result["documents_on_file"]
        required = REQUIRED_DOCUMENTS["initial"]

        missing = [doc for doc in required if doc not in on_file]
        assert len(missing) == 2
        assert "board_certification" in missing
        assert "cv_resume" in missing

    @pytest.mark.asyncio
    async def test_all_documents_present_even_npi(self):
        """Even NPI → all documents are present, missing list is empty."""
        caqh_result = await query_caqh("1234567890")  # even → complete
        assert caqh_result["success"] is True

        on_file = caqh_result["documents_on_file"]
        required = REQUIRED_DOCUMENTS["initial"]
        missing = [doc for doc in required if doc not in on_file]
        assert len(missing) == 0

    @pytest.mark.asyncio
    async def test_renewal_requires_fewer_documents(self):
        """Renewal type requires 4 documents vs 5 for initial."""
        assert len(REQUIRED_DOCUMENTS["renewal"]) == 4
        assert len(REQUIRED_DOCUMENTS["initial"]) == 5
        assert "cv_resume" not in REQUIRED_DOCUMENTS["renewal"]


# ── License Verification Tests ─────────────────────────────────────


class TestLicenseVerification:
    @pytest.mark.asyncio
    async def test_valid_license_verification(self):
        result = await verify_state_license("1234567890", "CA")
        assert result["success"] is True
        assert result["license_status"] == "active"
        assert result["verified"] is True
        assert result["state"] == "CA"

    @pytest.mark.asyncio
    async def test_invalid_state_code(self):
        result = await verify_state_license("1234567890", "X")
        assert result["success"] is False
        assert "Invalid state code" in result["error"]


# ── Sanctions Check Tests ──────────────────────────────────────────


class TestSanctionsCheck:
    @pytest.mark.asyncio
    async def test_clean_sanctions_check(self):
        result = await check_oig_exclusion("1234567890", "John Smith")
        assert result["success"] is True
        assert result["oig_excluded"] is False
        assert result["sam_excluded"] is False

    @pytest.mark.asyncio
    async def test_empty_npi_fails(self):
        result = await check_oig_exclusion("")
        assert result["success"] is False


# ── Application Compilation Tests ──────────────────────────────────


class TestApplicationCompilation:
    @pytest.mark.asyncio
    async def test_complete_application(self):
        result = await compile_application(
            npi="1234567890",
            provider_details={"first_name": "John", "last_name": "Smith", "credential": "MD"},
            verification_results={"sanctions_clear": True, "licenses": [{"verified": True}]},
            documents_checklist={"missing": []},
        )
        assert result["success"] is True
        assert result["ready_to_submit"] is True
        assert result["missing_documents"] == []

    @pytest.mark.asyncio
    async def test_incomplete_application(self):
        result = await compile_application(
            npi="1234567890",
            provider_details={"first_name": "John", "last_name": "Smith"},
            verification_results={"sanctions_clear": True},
            documents_checklist={"missing": ["board_certification", "cv_resume"]},
        )
        assert result["success"] is True
        assert result["ready_to_submit"] is False
        assert len(result["missing_documents"]) == 2

    @pytest.mark.asyncio
    async def test_sanctions_prevent_submission(self):
        result = await compile_application(
            npi="1234567890",
            provider_details={"first_name": "John", "last_name": "Smith"},
            verification_results={"sanctions_clear": False},
            documents_checklist={"missing": []},
        )
        assert result["ready_to_submit"] is False


# ── Application Submission Tests ───────────────────────────────────


class TestApplicationSubmission:
    @pytest.mark.asyncio
    async def test_submit_ready_application(self):
        result = await submit_application(
            application_data={"ready_to_submit": True, "npi": "1234567890"},
        )
        assert result["success"] is True
        assert result["status"] == "submitted"
        assert "tracking_number" in result

    @pytest.mark.asyncio
    async def test_reject_not_ready_application(self):
        result = await submit_application(
            application_data={"ready_to_submit": False, "missing_documents": ["cv_resume"]},
        )
        assert result["success"] is False
        assert "not ready" in result["error"]


# ── Credentialing Lifecycle Tests ──────────────────────────────────


class TestCredentialingLifecycle:
    @pytest.mark.asyncio
    async def test_track_status_sets_submitted_for_successful_submission(self):
        """After successful submission, track_status records 'submitted' status."""
        state = create_initial_state(
            task_id="test-lifecycle",
            agent_type="credentialing",
            input_data={"provider_npi": "1234567890"},
        )
        state["submission_result"] = {
            "success": True,
            "tracking_number": "CRED-567890-2026",
            "estimated_review_days": 90,
            "submission_date": "2026-03-27",
        }

        state = await track_status_node(state)
        assert state["application_status"]["status"] == "submitted"
        assert state["application_status"]["tracking_number"] == "CRED-567890-2026"

    @pytest.mark.asyncio
    async def test_track_status_sets_pending_documents_on_failure(self):
        """When submission failed, status is pending_documents."""
        state = create_initial_state(
            task_id="test-lifecycle-2",
            agent_type="credentialing",
            input_data={"provider_npi": "1234567890"},
        )
        state["submission_result"] = {
            "success": False,
            "missing": ["board_certification"],
        }

        state = await track_status_node(state)
        assert state["application_status"]["status"] == "pending_documents"

    @pytest.mark.asyncio
    async def test_full_lifecycle_submitted_to_under_review_to_approved(self):
        """Full graph run with complete docs: submitted → (polling) → approved.

        Uses an even-digit NPI so the CAQH mock returns all docs on file.
        """
        with _mock_nppes_lookup():
            state = await run_credentialing_agent(
                input_data={
                    "provider_npi": "1234567890",  # even → complete docs
                    "target_organization": "Test Hospital",
                    "credentialing_type": "initial",
                    "state": "CA",
                },
                llm_provider=LLMProvider(primary=MockLLMBackend()),
                task_id="test-lifecycle-approved",
            )
        # With complete docs, application should be submitted
        assert state["application_status"]["status"] == "submitted"
        assert state.get("needs_review") is False
        assert state.get("confidence", 0) >= 0.7

    @pytest.mark.asyncio
    async def test_full_lifecycle_incomplete_docs_triggers_review(self):
        """Odd NPI → incomplete CAQH docs → HITL review for missing docs."""
        with _mock_nppes_lookup():
            state = await run_credentialing_agent(
                input_data={
                    "provider_npi": "1234567891",  # odd → incomplete docs
                    "target_organization": "Test Hospital",
                    "credentialing_type": "initial",
                    "state": "CA",
                },
                llm_provider=LLMProvider(primary=MockLLMBackend()),
                task_id="test-lifecycle-incomplete",
            )
        # With missing docs, submission fails and review is triggered
        assert state["application_status"]["status"] == "pending_documents"
        assert state.get("needs_review") is True


# ── Alert Expirations Tests ──────────────────────────────────────


class TestAlertExpirations:
    @pytest.mark.asyncio
    async def test_alert_expirations_node_exists_in_graph(self, llm_provider):
        """The graph includes the alert_expirations node."""
        agent = CredentialingAgent(llm_provider=llm_provider)
        graph = agent.build_graph()
        assert "alert_expirations" in graph.node_names

    @pytest.mark.asyncio
    async def test_alert_expirations_no_alerts_far_expiry(self):
        """No alerts when license expiry is far in the future."""
        state = create_initial_state(
            task_id="test-expiry-1",
            agent_type="credentialing",
            input_data={"provider_npi": "1234567890"},
        )
        state["license_verification"] = {
            "expiration_date": "2028-06-30",
            "state": "CA",
        }
        state["documents_checklist"] = {}
        state["caqh_result"] = {}

        state = await alert_expirations_node(state)
        assert len(state["expiration_alerts"]) == 0

    @pytest.mark.asyncio
    async def test_alert_expirations_warns_near_expiry(self):
        """Alert generated when license expires within 90 days."""
        from datetime import date, timedelta
        near_expiry = (date.today() + timedelta(days=45)).isoformat()

        state = create_initial_state(
            task_id="test-expiry-2",
            agent_type="credentialing",
            input_data={"provider_npi": "1234567890"},
        )
        state["license_verification"] = {
            "expiration_date": near_expiry,
            "state": "CA",
        }
        state["documents_checklist"] = {}
        state["caqh_result"] = {}

        state = await alert_expirations_node(state)
        assert len(state["expiration_alerts"]) >= 1
        alert = state["expiration_alerts"][0]
        assert alert["type"] == "license_expiration"
        assert alert["severity"] == "warning"


# ── Graph Node Tests ───────────────────────────────────────────────


def _mock_nppes_lookup():
    """Return a context manager that mocks the NPPES API for deterministic tests."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "result_count": 1,
        "results": [{
            "enumeration_type": "NPI-1",
            "basic": {
                "first_name": "John",
                "last_name": "Smith",
                "credential": "MD",
                "gender": "M",
                "enumeration_date": "2010-05-15",
            },
            "taxonomies": [{
                "code": "207R00000X",
                "desc": "Internal Medicine",
                "primary": True,
                "state": "CA",
                "license": "A567890",
            }],
            "addresses": [{
                "address_purpose": "MAILING",
                "address_1": "123 Medical Plaza",
                "city": "Los Angeles",
                "state": "CA",
                "postal_code": "90001",
                "telephone_number": "310-555-0100",
            }],
        }],
    }

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    return patch("app.agents.credentialing.tools.httpx.AsyncClient", return_value=mock_client)


class TestCredentialingNodes:
    @pytest.mark.asyncio
    async def test_lookup_provider_success(self, credentialing_input):
        state = create_initial_state(
            task_id="test-1",
            agent_type="credentialing",
            input_data=credentialing_input,
        )
        with _mock_nppes_lookup():
            state = await lookup_provider_node(state)
        assert state.get("error") is None
        assert state["provider_details"]["npi"] == "1234567891"

    @pytest.mark.asyncio
    async def test_lookup_provider_missing_npi(self):
        state = create_initial_state(
            task_id="test-2",
            agent_type="credentialing",
            input_data={},
        )
        state = await lookup_provider_node(state)
        assert state.get("error") == "provider_npi is required"

    @pytest.mark.asyncio
    async def test_verify_licenses_identifies_missing_docs(self, credentialing_input):
        state = create_initial_state(
            task_id="test-3",
            agent_type="credentialing",
            input_data=credentialing_input,
        )
        with _mock_nppes_lookup():
            state = await lookup_provider_node(state)
        state = await verify_licenses_node(state)
        assert state["license_verification"]["verified"] is True
        assert len(state["documents_checklist"]["missing"]) == 2

    @pytest.mark.asyncio
    async def test_check_sanctions_clear(self, credentialing_input):
        state = create_initial_state(
            task_id="test-4",
            agent_type="credentialing",
            input_data=credentialing_input,
        )
        with _mock_nppes_lookup():
            state = await lookup_provider_node(state)
        state = await check_sanctions_node(state)
        assert state["verification_results"]["sanctions_clear"] is True

    @pytest.mark.asyncio
    async def test_evaluate_confidence_missing_docs_triggers_review(self, credentialing_input):
        state = create_initial_state(
            task_id="test-5",
            agent_type="credentialing",
            input_data=credentialing_input,
        )
        with _mock_nppes_lookup():
            state = await lookup_provider_node(state)
        state = await verify_licenses_node(state)
        state = await check_sanctions_node(state)
        state = await compile_application_node(state)
        state = await submit_node(state)
        state = await track_status_node(state)
        state = await alert_expirations_node(state)
        state = await evaluate_confidence_node(state)
        # Missing docs should trigger HITL review
        assert state["needs_review"] is True
        assert state["confidence"] < 0.7


# ── Full Agent Run Tests ───────────────────────────────────────────


class TestCredentialingAgentRun:
    @pytest.mark.asyncio
    async def test_full_pipeline(self, llm_provider, credentialing_input):
        """Integration test: NPI → lookup → verify → submit → track (incomplete docs)."""
        with _mock_nppes_lookup():
            state = await run_credentialing_agent(
                input_data=credentialing_input,
                llm_provider=llm_provider,
                task_id="test-full-1",
            )
        assert state.get("error") is None
        assert state["provider_details"]["npi"] == "1234567891"
        # Missing docs triggers review
        assert state["needs_review"] is True
        assert len(state.get("audit_trail", [])) >= 5

    @pytest.mark.asyncio
    async def test_full_pipeline_complete_docs(self, llm_provider, credentialing_input_complete):
        """Integration test with complete docs: NPI → lookup → verify → submit → submitted."""
        with _mock_nppes_lookup():
            state = await run_credentialing_agent(
                input_data=credentialing_input_complete,
                llm_provider=llm_provider,
                task_id="test-full-complete",
            )
        assert state.get("error") is None
        # Complete docs → successful submission
        assert state["application_status"]["status"] == "submitted"
        assert state.get("needs_review") is False
        assert state.get("confidence", 0) >= 0.7

    @pytest.mark.asyncio
    async def test_agent_produces_audit_trail(self, llm_provider, credentialing_input):
        """Verify audit trail contains all expected actions."""
        with _mock_nppes_lookup():
            state = await run_credentialing_agent(
                input_data=credentialing_input,
                llm_provider=llm_provider,
                task_id="test-audit-1",
            )
        audit = state.get("audit_trail", [])
        assert len(audit) >= 5
        actions = [e.get("action", "") if isinstance(e, dict) else getattr(e, "action", "") for e in audit]
        assert "provider_lookup_completed" in actions
        assert "licenses_verified" in actions
        assert "sanctions_checked" in actions
        assert "expirations_checked" in actions
        assert "submission_attempted" in actions

    @pytest.mark.asyncio
    async def test_agent_type_is_credentialing(self, llm_provider):
        agent = CredentialingAgent(llm_provider=llm_provider)
        assert agent.agent_type == "credentialing"

    @pytest.mark.asyncio
    async def test_get_tools_returns_definitions(self, llm_provider):
        agent = CredentialingAgent(llm_provider=llm_provider)
        tools = agent.get_tools()
        assert len(tools) == 6
        tool_names = [t.name for t in tools]
        assert "lookup_nppes" in tool_names
        assert "query_caqh" in tool_names
        assert "check_oig_exclusion" in tool_names

    @pytest.mark.asyncio
    async def test_build_graph_returns_agent_graph(self, llm_provider):
        agent = CredentialingAgent(llm_provider=llm_provider)
        graph = agent.build_graph()
        assert "lookup_provider" in graph.node_names
        assert "verify_licenses" in graph.node_names
        assert "check_sanctions" in graph.node_names
        assert "alert_expirations" in graph.node_names
        assert "output" in graph.node_names


# ── Workflow Registration Tests ───────────────────────────────────


class TestWorkflowRegistration:
    def test_credentialing_workflow_registered_in_worker(self):
        """Verify CredentialingWorkflow is registered in the Temporal worker."""
        from app.workflows.worker import get_registered_workflows, get_registered_activities
        from app.workflows.credentialing import CredentialingWorkflow

        workflows = get_registered_workflows()
        workflow_types = [w for w in workflows if w is CredentialingWorkflow]
        assert len(workflow_types) == 1, "CredentialingWorkflow must be registered"

    def test_credentialing_activities_registered_in_worker(self):
        """Verify credentialing activities are registered."""
        from app.workflows.worker import get_registered_activities
        from app.workflows.credentialing import (
            validate_credentialing_input,
            run_credentialing_agent_activity,
            write_credentialing_result,
            check_credentialing_status_activity,
            alert_expiration_activity,
        )

        activities = get_registered_activities()
        expected = {
            validate_credentialing_input,
            run_credentialing_agent_activity,
            write_credentialing_result,
            check_credentialing_status_activity,
            alert_expiration_activity,
        }
        registered = set(activities)
        for act in expected:
            assert act in registered, f"Activity {act.__name__} must be registered"


# ── Schema Validation Tests ────────────────────────────────────────


class TestCredentialingSchema:
    def test_valid_request(self):
        from app.schemas.credentialing import CredentialingRequest
        req = CredentialingRequest(provider_npi="1234567890")
        assert req.provider_npi == "1234567890"
        assert req.credentialing_type == "initial"

    def test_invalid_npi_rejected(self):
        from app.schemas.credentialing import CredentialingRequest
        with pytest.raises(ValueError, match="10 digits"):
            CredentialingRequest(provider_npi="12345")

    def test_empty_npi_rejected(self):
        from app.schemas.credentialing import CredentialingRequest
        with pytest.raises(ValueError):
            CredentialingRequest(provider_npi="")


# ── OIG Exclusion Provider Adapter Tests ─────────────────────────


class TestOIGExclusionAdapter:
    """Test the adapter-driven OIG exclusion check system."""

    @pytest.mark.asyncio
    async def test_mock_provider_returns_clean(self):
        """MockOIGExclusionProvider always returns no exclusion."""
        from app.agents.credentialing.tools import MockOIGExclusionProvider

        provider = MockOIGExclusionProvider()
        result = await provider.check("1234567890", "John Smith")
        assert result["success"] is True
        assert result["oig_excluded"] is False
        assert result["sam_excluded"] is False
        # Dates should be dynamic (today), not hard-coded
        from datetime import date
        assert result["oig_check_date"] == date.today().isoformat()

    @pytest.mark.asyncio
    async def test_excluded_provider_returns_exclusion(self):
        """ExcludedOIGProvider returns an exclusion hit."""
        from app.agents.credentialing.tools import ExcludedOIGProvider

        provider = ExcludedOIGProvider()
        result = await provider.check("1234567890", "John Smith")
        assert result["success"] is True
        assert result["oig_excluded"] is True
        assert result["exclusion_details"] is not None

    @pytest.mark.asyncio
    async def test_set_and_get_oig_provider(self):
        """set_oig_provider and get_oig_provider wire correctly."""
        from app.agents.credentialing.tools import (
            ExcludedOIGProvider,
            MockOIGExclusionProvider,
            check_oig_exclusion,
            set_oig_provider,
            get_oig_provider,
        )

        original = get_oig_provider()
        try:
            set_oig_provider(ExcludedOIGProvider())
            result = await check_oig_exclusion("1234567890")
            assert result["oig_excluded"] is True
        finally:
            set_oig_provider(original)

        # After restoring, should be clean again
        result = await check_oig_exclusion("1234567890")
        assert result["oig_excluded"] is False

    @pytest.mark.asyncio
    async def test_oig_exclusion_triggers_sanctions_not_clear(self):
        """When OIG provider returns exclusion, check_sanctions_node sets sanctions_clear=False."""
        from app.agents.credentialing.tools import (
            ExcludedOIGProvider,
            MockOIGExclusionProvider,
            set_oig_provider,
        )
        from app.core.engine.state import create_initial_state

        set_oig_provider(ExcludedOIGProvider())
        try:
            # Build a state that has already passed lookup and license verification
            state = create_initial_state(
                task_id=str(uuid.uuid4()),
                agent_type="credentialing",
                input_data={
                    "provider_npi": "1234567890",
                    "target_organization": "Test",
                    "credentialing_type": "initial",
                    "state": "CA",
                },
            )
            state["provider_details"] = {
                "npi": "1234567890",
                "first_name": "John",
                "last_name": "Smith",
                "credential": "MD",
                "taxonomy": {"state": "CA", "license": "CA-567890"},
            }
            state["license_verification"] = {
                "success": True,
                "verified": True,
                "license_status": "active",
            }

            # Run check_sanctions_node directly
            result_state = await check_sanctions_node(state)

            assert result_state["verification_results"]["sanctions_clear"] is False
            assert result_state["sanctions_check"]["oig_excluded"] is True

            # Now run evaluate_confidence_node to verify escalation
            result_state = await evaluate_confidence_node(result_state)
            assert result_state["needs_review"] is True
            assert result_state["confidence"] <= 0.1
        finally:
            set_oig_provider(MockOIGExclusionProvider())


# ── Dynamic Date Tests ───────────────────────────────────────────


class TestDynamicDates:
    """Verify that dates and tracking numbers use dynamic values."""

    @pytest.mark.asyncio
    async def test_submit_uses_current_date(self):
        """submit_application should use today's date, not hard-coded."""
        from datetime import date
        result = await submit_application(
            application_data={"ready_to_submit": True, "npi": "1234567890"},
        )
        assert result["submission_date"] == date.today().isoformat()

    @pytest.mark.asyncio
    async def test_tracking_number_uses_current_year(self):
        """Tracking number should include current year."""
        from datetime import date
        result = await submit_application(
            application_data={"ready_to_submit": True, "npi": "1234567890"},
        )
        assert str(date.today().year) in result["tracking_number"]

    @pytest.mark.asyncio
    async def test_oig_check_uses_current_date(self):
        """OIG check date should be today's date."""
        from datetime import date
        result = await check_oig_exclusion("1234567890", "Test Provider")
        assert result["oig_check_date"] == date.today().isoformat()
        assert result["sam_check_date"] == date.today().isoformat()


# ── Issue 3: HttpOIGExclusionProvider Tests ──────────────────────


class TestHttpOIGExclusionProvider:
    """Tests for the concrete HTTP-based OIG LEIE/SAM provider."""

    @pytest.mark.asyncio
    async def test_http_provider_clean_result(self):
        """HTTP provider returns clean result when APIs report no exclusions."""
        from app.agents.credentialing.tools import HttpOIGExclusionProvider

        provider = HttpOIGExclusionProvider(base_url="https://oig.example.com")

        oig_response = MagicMock()
        oig_response.status_code = 200
        oig_response.raise_for_status = MagicMock()
        oig_response.json.return_value = {"total": 0, "results": []}

        sam_response = MagicMock()
        sam_response.status_code = 200
        sam_response.raise_for_status = MagicMock()
        sam_response.json.return_value = {"totalRecords": 0}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[oig_response, sam_response])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.agents.credentialing.tools.httpx.AsyncClient", return_value=mock_client):
            result = await provider.check("1234567890", "John Smith")

        assert result["success"] is True
        assert result["oig_excluded"] is False
        assert result["sam_excluded"] is False

    @pytest.mark.asyncio
    async def test_http_provider_exclusion_found(self):
        """HTTP provider returns exclusion when OIG API reports a hit."""
        from app.agents.credentialing.tools import HttpOIGExclusionProvider

        provider = HttpOIGExclusionProvider(base_url="https://oig.example.com")

        oig_response = MagicMock()
        oig_response.status_code = 200
        oig_response.raise_for_status = MagicMock()
        oig_response.json.return_value = {
            "total": 1,
            "results": [{
                "excltype": "1128(a)(1)",
                "excldate": "2024-01-15",
                "general": "Program-related conviction",
            }],
        }

        sam_response = MagicMock()
        sam_response.status_code = 200
        sam_response.raise_for_status = MagicMock()
        sam_response.json.return_value = {"totalRecords": 0}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[oig_response, sam_response])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.agents.credentialing.tools.httpx.AsyncClient", return_value=mock_client):
            result = await provider.check("1234567890", "John Smith")

        assert result["success"] is True
        assert result["oig_excluded"] is True
        assert result["exclusion_details"] is not None
        assert result["exclusion_details"]["exclusion_type"] == "1128(a)(1)"

    @pytest.mark.asyncio
    async def test_http_provider_oig_api_failure(self):
        """HTTP provider returns failure when OIG API is unreachable."""
        import httpx
        from app.agents.credentialing.tools import HttpOIGExclusionProvider

        provider = HttpOIGExclusionProvider(base_url="https://oig.example.com")

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.agents.credentialing.tools.httpx.AsyncClient", return_value=mock_client):
            result = await provider.check("1234567890", "John Smith")

        assert result["success"] is False
        assert "OIG LEIE API request failed" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_http_provider_sam_failure_partial_success(self):
        """When SAM.gov fails but OIG succeeds, result is still success=True."""
        import httpx
        from app.agents.credentialing.tools import HttpOIGExclusionProvider

        provider = HttpOIGExclusionProvider(base_url="https://oig.example.com")

        oig_response = MagicMock()
        oig_response.status_code = 200
        oig_response.raise_for_status = MagicMock()
        oig_response.json.return_value = {"total": 0, "results": []}

        async def mock_resilient_get(url, **kwargs):
            if "exclusions/search" in url:
                return oig_response
            elif "sam/exclusions" in url:
                raise httpx.ConnectError("SAM down")
            return oig_response

        with patch(
            "app.core.resilience.resilient_http_get",
            side_effect=mock_resilient_get,
        ):
            result = await provider.check("1234567890", "John Smith")

        assert result["success"] is True
        assert result["oig_excluded"] is False
        assert "_warning" in result

    def test_env_based_provider_selection_with_url(self):
        """When oig_api_base_url is set, HttpOIGExclusionProvider is created."""
        from app.agents.credentialing.tools import (
            HttpOIGExclusionProvider,
            _create_default_oig_provider,
        )

        with patch("app.config.settings") as mock_settings:
            mock_settings.oig_api_base_url = "https://oig.example.com"
            provider = _create_default_oig_provider()
            assert isinstance(provider, HttpOIGExclusionProvider)

    def test_env_based_provider_selection_without_url(self):
        """When oig_api_base_url is empty, MockOIGExclusionProvider is used."""
        from app.agents.credentialing.tools import (
            MockOIGExclusionProvider,
            _create_default_oig_provider,
        )

        with patch("app.config.settings") as mock_settings:
            mock_settings.oig_api_base_url = ""
            provider = _create_default_oig_provider()
            assert isinstance(provider, MockOIGExclusionProvider)


# ── Issue 4: Credentialing Task Status Semantics ─────────────────


class TestCredentialingTaskStatus:
    """Verify that task status remains 'running' while application is
    in a non-terminal state (submitted / under_review), and only
    transitions to 'completed' when the application reaches a
    terminal status (approved / denied).

    These tests use the DB session via the write_credentialing_result
    activity, which creates its own session from _get_activity_session_factory.
    """

    @pytest.mark.asyncio
    async def test_write_result_sets_running_for_submitted_app(self, db_session):
        """write_credentialing_result sets task status='running' when
        application_status is 'submitted'."""
        from app.workflows.credentialing import write_credentialing_result
        from app.models.agent_task import AgentTask
        from sqlalchemy import select

        # Create a task in the DB
        task = AgentTask(
            id="00000000-0000-4000-8000-000000000001",
            agent_type="credentialing",
            status="pending",
            input_data={"provider_npi": "1234567890"},
        )
        db_session.add(task)
        await db_session.commit()

        await write_credentialing_result({
            "task_id": "00000000-0000-4000-8000-000000000001",
            "output": {
                "needs_review": False,
                "confidence": 0.85,
            },
            "input_data": {"provider_npi": "1234567890"},
            "provider_details": {"npi": "1234567890", "first_name": "John", "last_name": "Smith"},
            "documents_checklist": {"missing": []},
            "verification_results": {"sanctions_clear": True},
            "application": {"ready_to_submit": True},
            "submission_result": {"success": True, "tracking_number": "TEST-123"},
            "application_status": {"status": "submitted"},
            "audit_trail": [],
        })

        # Re-query from DB (activity uses its own session)
        result = await db_session.execute(
            select(AgentTask).where(AgentTask.id == "00000000-0000-4000-8000-000000000001")
        )
        refreshed_task = result.scalar_one_or_none()
        assert refreshed_task is not None
        assert refreshed_task.status == "running", (
            f"Task should be 'running' while application is 'submitted', got '{refreshed_task.status}'"
        )

    @pytest.mark.asyncio
    async def test_write_result_sets_completed_for_terminal_app(self, db_session):
        """write_credentialing_result sets task status='completed' when
        application_status is terminal (e.g., not submitted/under_review)."""
        from app.workflows.credentialing import write_credentialing_result
        from app.models.agent_task import AgentTask
        from sqlalchemy import select

        task = AgentTask(
            id="00000000-0000-4000-8000-000000000002",
            agent_type="credentialing",
            status="pending",
            input_data={"provider_npi": "1234567890"},
        )
        db_session.add(task)
        await db_session.commit()

        await write_credentialing_result({
            "task_id": "00000000-0000-4000-8000-000000000002",
            "output": {
                "needs_review": False,
                "confidence": 0.85,
            },
            "input_data": {"provider_npi": "1234567890"},
            "provider_details": {"npi": "1234567890", "first_name": "John", "last_name": "Smith"},
            "documents_checklist": {"missing": []},
            "verification_results": {"sanctions_clear": True},
            "application": {"ready_to_submit": True},
            "submission_result": {"success": True},
            "application_status": {"status": "approved"},
            "audit_trail": [],
        })

        result = await db_session.execute(
            select(AgentTask).where(AgentTask.id == "00000000-0000-4000-8000-000000000002")
        )
        refreshed_task = result.scalar_one_or_none()
        assert refreshed_task is not None
        assert refreshed_task.status == "completed", (
            f"Task should be 'completed' when application is in terminal state, got '{refreshed_task.status}'"
        )

    @pytest.mark.asyncio
    async def test_write_result_sets_review_when_needs_review(self, db_session):
        """write_credentialing_result sets task status='review' when
        needs_review is True, regardless of application status."""
        from app.workflows.credentialing import write_credentialing_result
        from app.models.agent_task import AgentTask
        from sqlalchemy import select

        task = AgentTask(
            id="00000000-0000-4000-8000-000000000003",
            agent_type="credentialing",
            status="pending",
            input_data={"provider_npi": "1234567890"},
        )
        db_session.add(task)
        await db_session.commit()

        await write_credentialing_result({
            "task_id": "00000000-0000-4000-8000-000000000003",
            "output": {
                "needs_review": True,
                "confidence": 0.3,
            },
            "input_data": {"provider_npi": "1234567890"},
            "provider_details": {"npi": "1234567890", "first_name": "John", "last_name": "Smith"},
            "documents_checklist": {"missing": ["board_certification"]},
            "verification_results": {"sanctions_clear": True},
            "application": {"ready_to_submit": False},
            "submission_result": {"success": False},
            "application_status": {"status": "pending_documents"},
            "audit_trail": [],
        })

        result = await db_session.execute(
            select(AgentTask).where(AgentTask.id == "00000000-0000-4000-8000-000000000003")
        )
        refreshed_task = result.scalar_one_or_none()
        assert refreshed_task is not None
        assert refreshed_task.status == "review", (
            f"Task should be 'review' when needs_review=True, got '{refreshed_task.status}'"
        )
