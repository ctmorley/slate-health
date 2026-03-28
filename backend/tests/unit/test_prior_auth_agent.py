"""Unit tests for the Prior Authorization Agent.

Tests cover: PA requirement checking, clinical document gathering, X12 278
building, PA submission and status tracking, denial handling, appeal letter
generation, Da Vinci PAS format builder, RPA stub, and full agent lifecycle.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.prior_auth.graph import (
    PriorAuthAgent,
    check_pa_required_node,
    gather_clinical_docs_node,
    build_pa_request_node,
    determine_submission_channel_node,
    submit_pa_node,
    track_status_node,
    handle_denial_node,
    generate_appeal_node,
    evaluate_confidence_node,
    escalate_node,
    output_node,
)
from app.agents.prior_auth.tools import (
    check_pa_required,
    gather_clinical_documents,
    build_278_request,
    parse_278_response,
    submit_pa_to_clearinghouse,
    poll_pa_status,
    generate_appeal_letter,
    build_davinci_pas_request,
    get_prior_auth_tools,
    PA_EXEMPT_PROCEDURES,
)
from app.agents.prior_auth.rpa_stub import (
    PortalAutomationBase,
    RPANotImplementedError,
    submit_via_portal,
)
from app.core.engine.llm_provider import LLMProvider, MockLLMBackend
from app.core.engine.state import create_initial_state


# ── Helpers ────────────────────────────────────────────────────────────

def _make_state(**overrides: Any) -> dict:
    """Create a fresh prior auth state dict with sensible defaults."""
    input_data = overrides.pop("input_data", {
        "procedure_code": "27447",
        "diagnosis_codes": ["M17.11", "M25.561"],
        "subscriber_id": "MEM-12345",
        "subscriber_first_name": "John",
        "subscriber_last_name": "Smith",
        "subscriber_dob": "19650315",
        "payer_id": "BCBS01",
        "payer_name": "Blue Cross Blue Shield",
        "provider_npi": "1234567890",
        "provider_name": "Dr. Johnson",
        "patient_id": "PAT-001",
        "date_of_service": "20250401",
    })
    state = create_initial_state(
        task_id=overrides.pop("task_id", str(uuid.uuid4())),
        agent_type="prior_auth",
        input_data=input_data,
    )
    state_dict = dict(state)
    state_dict.update(overrides)
    return state_dict


# ── PA Requirement Check Tests ────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_pa_required_known_procedure():
    """PA required for a known procedure code (27447 — knee replacement)."""
    result = await check_pa_required(
        procedure_code="27447",
        payer_id="BCBS01",
        diagnosis_codes=["M17.11"],
    )
    assert result["pa_required"] is True
    assert result["procedure_code"] == "27447"
    assert result["payer_rules_checked"] is True
    assert "clinical_docs_needed" in result
    assert len(result["clinical_docs_needed"]) > 0


@pytest.mark.asyncio
async def test_check_pa_required_exempt_procedure():
    """PA NOT required for exempt procedure (99213 — office visit)."""
    result = await check_pa_required(
        procedure_code="99213",
        payer_id="BCBS01",
    )
    assert result["pa_required"] is False
    assert result["reason"] == "exempt_procedure"


@pytest.mark.asyncio
async def test_check_pa_required_mri():
    """PA required for MRI procedures."""
    result = await check_pa_required(
        procedure_code="72148",
        payer_id="AETNA01",
    )
    assert result["pa_required"] is True


@pytest.mark.asyncio
async def test_check_pa_required_unknown_defaults_to_required():
    """Unknown procedure codes default to requiring PA for safety."""
    result = await check_pa_required(
        procedure_code="99999",
        payer_id="UNKNOWN_PAYER",
    )
    assert result["pa_required"] is True
    assert "unknown_procedure" in result["reason"]


@pytest.mark.asyncio
async def test_check_pa_required_five_combinations():
    """Test 5 procedure+payer combinations with known outcomes (3 required, 2 exempt)."""
    # Required: knee replacement
    r1 = await check_pa_required(procedure_code="27447", payer_id="BCBS01")
    assert r1["pa_required"] is True

    # Required: MRI spine
    r2 = await check_pa_required(procedure_code="72158", payer_id="AETNA01")
    assert r2["pa_required"] is True

    # Required: spine fusion
    r3 = await check_pa_required(procedure_code="22551", payer_id="UHC01")
    assert r3["pa_required"] is True

    # Exempt: office visit
    r4 = await check_pa_required(procedure_code="99214", payer_id="BCBS01")
    assert r4["pa_required"] is False

    # Exempt: venipuncture
    r5 = await check_pa_required(procedure_code="36415", payer_id="BCBS01")
    assert r5["pa_required"] is False


# ── Clinical Document Gathering Tests ─────────────────────────────────


@pytest.mark.asyncio
async def test_gather_clinical_docs_mock():
    """Clinical doc gathering returns conditions, meds, labs, procedures (mock)."""
    result = await gather_clinical_documents(patient_id="PAT-001")
    assert result["success"] is True
    assert len(result["conditions"]) >= 3
    assert len(result["medications"]) >= 5
    assert len(result["lab_results"]) >= 2
    assert len(result["recent_procedures"]) >= 1
    assert result["document_count"] > 0


@pytest.mark.asyncio
async def test_gather_clinical_docs_structure():
    """Clinical docs have the expected field structure."""
    result = await gather_clinical_documents(patient_id="PAT-001")
    # Check condition structure
    for condition in result["conditions"]:
        assert "code" in condition
        assert "display" in condition
        assert "onset" in condition
        assert "status" in condition

    # Check medication structure
    for med in result["medications"]:
        assert "code" in med
        assert "display" in med
        assert "status" in med


# ── X12 278 Building Tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_278_request_success():
    """build_278_request returns valid X12 278 payload."""
    result = await build_278_request(
        provider_npi="1234567890",
        provider_name="Dr. Johnson",
        subscriber_id="MEM-12345",
        subscriber_first_name="John",
        subscriber_last_name="Smith",
        subscriber_dob="19650315",
        payer_id="BCBS01",
        payer_name="Blue Cross Blue Shield",
        procedure_code="27447",
        diagnosis_codes=["M17.11", "M25.561"],
        date_of_service="20250401",
    )
    assert result["success"] is True
    assert "x12_278" in result
    assert "control_number" in result
    # Verify it's a valid X12 string with expected segments
    x12 = result["x12_278"]
    assert "ISA" in x12
    assert "278" in x12
    assert "27447" in x12  # procedure code present
    assert "M17.11" in x12  # diagnosis code present


@pytest.mark.asyncio
async def test_build_278_request_with_clinical_info():
    """build_278_request includes diagnosis codes and PWK clinical attachment segments."""
    clinical_evidence = {
        "conditions": [
            {"code": "M54.5", "display": "Low back pain", "onset": "2024-06-01", "status": "active"},
        ],
        "medications": [
            {"code": "310798", "display": "Meloxicam 15mg", "status": "active", "date_prescribed": "2024-03-01"},
        ],
        "lab_results": [
            {"code": "2160-0", "display": "Creatinine", "value": "0.9", "unit": "mg/dL", "date": "2024-11-01"},
        ],
        "recent_procedures": [
            {"code": "97110", "display": "Physical therapy", "date": "2024-04-01", "outcome": "limited_improvement"},
        ],
        "document_count": 4,
    }
    result = await build_278_request(
        provider_npi="1234567890",
        provider_name="Dr. Johnson",
        subscriber_id="MEM-001",
        subscriber_first_name="Jane",
        subscriber_last_name="Doe",
        subscriber_dob="19800101",
        payer_id="AETNA01",
        payer_name="Aetna",
        procedure_code="72148",
        diagnosis_codes=["M54.5", "M79.3"],
        date_of_service="20250501",
        place_of_service="22",
        clinical_evidence=clinical_evidence,
    )
    assert result["success"] is True
    x12 = result["x12_278"]
    assert "M54.5" in x12
    assert "M79.3" in x12
    # Verify PWK (Paperwork) segments are present for clinical attachments
    assert "PWK" in x12
    # Verify MSG segments carry clinical descriptions
    assert "MSG" in x12
    assert "Clinical conditions" in x12 or "Low back pain" in x12


# ── PA Submission and Status Tracking Tests ───────────────────────────


@pytest.mark.asyncio
async def test_submit_pa_mock():
    """Mock clearinghouse submission returns pending status."""
    result = await submit_pa_to_clearinghouse(
        x12_278="ISA*...",
        payer_id="BCBS01",
        control_number="123456789",
    )
    assert result["success"] is True
    assert result["status"] == "pending"
    assert result["transaction_id"].startswith("PA-")


@pytest.mark.asyncio
async def test_poll_pa_status():
    """Status polling returns authorization details."""
    result = await poll_pa_status(
        transaction_id="PA-ABC123",
        payer_id="BCBS01",
    )
    assert result["success"] is True
    # Mock status is determined by stable SHA-256 hash of transaction_id.
    # Valid terminal statuses exclude "pending" (which means still in progress).
    assert result["status"] in ("approved", "denied", "pended", "cancelled")
    assert "authorization_number" in result


@pytest.mark.asyncio
async def test_submit_then_approved_flow():
    """Submit → pending → approved lifecycle flow."""
    # Submit
    submit_result = await submit_pa_to_clearinghouse(
        x12_278="ISA*...",
        payer_id="BCBS01",
    )
    assert submit_result["success"] is True
    assert submit_result["status"] == "pending"

    # Poll status (force approved for deterministic test)
    status = await poll_pa_status(
        transaction_id=submit_result["transaction_id"],
        payer_id="BCBS01",
        _force_status="approved",
    )
    assert status["success"] is True
    assert status["status"] == "approved"
    assert status["authorization_number"].startswith("AUTH-")


# ── Denial and Appeal Tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_appeal_letter():
    """Appeal letter includes diagnosis, procedure, clinical evidence."""
    result = await generate_appeal_letter(
        patient_name="John Smith",
        patient_dob="1965-03-15",
        procedure_code="27447",
        procedure_description="Total knee replacement, right",
        diagnosis_codes=["M17.11", "M25.561"],
        payer_name="Blue Cross Blue Shield",
        auth_number="AUTH-DENIED-001",
        denial_reason="Medical necessity not established",
        denial_date="2025-03-15",
        clinical_evidence={
            "conditions": [
                {"code": "M17.11", "display": "Primary osteoarthritis, right knee",
                 "onset": "2024-01-15", "status": "active"},
            ],
            "medications": [
                {"code": "860092", "display": "Ibuprofen 800mg",
                 "status": "active", "date_prescribed": "2024-06-01"},
            ],
            "lab_results": [
                {"code": "2160-0", "display": "Creatinine",
                 "value": "0.9", "unit": "mg/dL", "date": "2024-11-01"},
            ],
            "recent_procedures": [
                {"code": "20610", "display": "Corticosteroid injection, right knee",
                 "date": "2024-06-15", "outcome": "temporary_relief"},
            ],
        },
        payer_policy_reference="Medical Policy #KR-001",
    )

    assert result["success"] is True
    letter = result["appeal_letter"]

    # Verify letter content — specific clinical evidence references
    assert "John Smith" in letter
    assert "27447" in letter
    assert "Total knee replacement" in letter
    assert "M17.11" in letter
    assert "AUTH-DENIED-001" in letter
    assert "Medical necessity not established" in letter
    assert "osteoarthritis" in letter.lower() or "Corticosteroid injection" in letter
    assert "Ibuprofen" in letter
    assert "Creatinine" in letter
    assert "Policy" in letter  # policy reference section

    # Verify evidence citation counts
    evidence_cited = result["evidence_cited"]
    assert evidence_cited["conditions_referenced"] >= 1
    assert evidence_cited["medications_referenced"] >= 1
    assert evidence_cited["labs_referenced"] >= 1
    assert evidence_cited["procedures_referenced"] >= 1


@pytest.mark.asyncio
async def test_appeal_letter_includes_clinical_citations():
    """Appeal letter specifically references clinical evidence supporting necessity."""
    result = await generate_appeal_letter(
        patient_name="Jane Doe",
        patient_dob="1980-05-20",
        procedure_code="72148",
        procedure_description="MRI lumbar spine without contrast",
        diagnosis_codes=["M54.5"],
        payer_name="Aetna",
        auth_number="AUTH-789",
        denial_reason="Insufficient documentation",
        denial_date="2025-03-20",
        clinical_evidence={
            "conditions": [
                {"code": "M54.5", "display": "Low back pain",
                 "onset": "2024-06-01", "status": "active"},
            ],
            "medications": [
                {"code": "310798", "display": "Meloxicam 15mg",
                 "status": "stopped", "date_prescribed": "2024-03-01"},
            ],
            "lab_results": [],
            "recent_procedures": [
                {"code": "97110", "display": "Physical therapy",
                 "date": "2024-04-01", "outcome": "limited_improvement"},
            ],
        },
    )

    assert result["success"] is True
    letter = result["appeal_letter"]
    assert "Low back pain" in letter
    assert "Physical therapy" in letter
    assert "72148" in letter


# ── Da Vinci PAS Format Tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_davinci_pas_request_structure():
    """Da Vinci PAS builder produces valid FHIR Claim resource."""
    result = await build_davinci_pas_request(
        patient_id="PAT-001",
        provider_npi="1234567890",
        payer_id="BCBS01",
        procedure_code="27447",
        diagnosis_codes=["M17.11", "M25.561"],
        date_of_service="2025-04-01",
    )

    assert result["success"] is True
    assert result["format"] == "davinci-pas"
    assert result["ig_version"] == "2.0.1"

    claim = result["fhir_claim"]
    assert claim["resourceType"] == "Claim"
    assert claim["use"] == "preauthorization"
    assert claim["status"] == "active"

    # Check patient reference
    assert claim["patient"]["reference"] == "Patient/PAT-001"

    # Check provider NPI
    assert claim["provider"]["identifier"]["value"] == "1234567890"

    # Check diagnosis codes
    assert len(claim["diagnosis"]) == 2
    assert claim["diagnosis"][0]["diagnosisCodeableConcept"]["coding"][0]["code"] == "M17.11"

    # Check procedure item
    assert len(claim["item"]) == 1
    assert claim["item"][0]["productOrService"]["coding"][0]["code"] == "27447"
    assert claim["item"][0]["servicedDate"] == "2025-04-01"


@pytest.mark.asyncio
async def test_davinci_pas_with_clinical_info():
    """Da Vinci PAS includes clinical supporting info."""
    result = await build_davinci_pas_request(
        patient_id="PAT-002",
        provider_npi="9876543210",
        payer_id="UHC01",
        procedure_code="72148",
        diagnosis_codes=["M54.5"],
        date_of_service="2025-05-01",
        clinical_info={
            "conditions": [
                {"code": "M54.5", "display": "Low back pain"},
            ],
        },
    )

    assert result["success"] is True
    claim = result["fhir_claim"]
    assert len(claim["supportingInfo"]) >= 1
    assert claim["supportingInfo"][0]["category"]["coding"][0]["code"] == "patientDiagnosis"


@pytest.mark.asyncio
async def test_davinci_pas_normalizes_yyyymmdd_date():
    """Da Vinci PAS builder normalizes YYYYMMDD → YYYY-MM-DD for servicedDate."""
    result = await build_davinci_pas_request(
        patient_id="PAT-003",
        provider_npi="1234567890",
        payer_id="BCBS01",
        procedure_code="27447",
        diagnosis_codes=["M17.11"],
        date_of_service="20250401",  # YYYYMMDD input
    )

    assert result["success"] is True
    claim = result["fhir_claim"]
    # servicedDate must be FHIR-compliant YYYY-MM-DD, not YYYYMMDD
    assert claim["item"][0]["servicedDate"] == "2025-04-01"


@pytest.mark.asyncio
async def test_appeal_letter_includes_policy_section_without_reference():
    """Appeal letter includes a payer policy section even without explicit policy reference."""
    result = await generate_appeal_letter(
        patient_name="Jane Doe",
        patient_dob="1980-05-20",
        procedure_code="72148",
        procedure_description="MRI lumbar spine without contrast",
        diagnosis_codes=["M54.5"],
        payer_name="Aetna",
        auth_number="AUTH-789",
        denial_reason="Insufficient documentation",
        denial_date="2025-03-20",
        clinical_evidence={
            "conditions": [
                {"code": "M54.5", "display": "Low back pain",
                 "onset": "2024-06-01", "status": "active"},
            ],
            "medications": [],
            "lab_results": [],
            "recent_procedures": [],
        },
        payer_policy_reference="",  # No policy reference
    )

    assert result["success"] is True
    letter = result["appeal_letter"]
    # Must still have a payer policy section
    assert "PAYER POLICY REFERENCE" in letter
    assert "Aetna" in letter
    assert "human-in-the-loop" in letter.lower() or "reviewer" in letter.lower()


# ── RPA Stub Tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rpa_stub_submit_via_portal():
    """RPA stub returns structured NotImplemented with guidance."""
    result = await submit_via_portal(
        payer_id="BCBS01",
        patient_name="John Smith",
        patient_dob="1965-03-15",
        subscriber_id="MEM-12345",
        procedure_code="27447",
        diagnosis_codes=["M17.11"],
    )
    assert result["success"] is False
    assert "not yet implemented" in result["error"].lower()
    assert result["submission_channel"] == "portal"
    assert "fallback_options" in result
    assert "clearinghouse_278" in result["fallback_options"]
    assert "implementation_guidance" in result


@pytest.mark.asyncio
async def test_rpa_portal_automation_base_raises():
    """PortalAutomationBase methods raise RPANotImplementedError."""
    portal = PortalAutomationBase()
    portal.payer_id = "TEST_PAYER"
    portal.portal_url = "https://portal.example.com"

    with pytest.raises(RPANotImplementedError):
        await portal.authenticate({"username": "test", "password": "test"})

    with pytest.raises(RPANotImplementedError):
        await portal.submit_pa_request(
            patient_name="Test",
            patient_dob="2000-01-01",
            subscriber_id="SUB-001",
            procedure_code="27447",
            diagnosis_codes=["M17.11"],
        )

    with pytest.raises(RPANotImplementedError):
        await portal.check_pa_status("REF-001")

    with pytest.raises(RPANotImplementedError):
        await portal.upload_appeal_documents("REF-001", [])


# ── Graph Node Tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_pa_required_node_required():
    """check_pa_required_node correctly identifies PA is required."""
    state = _make_state()
    result = await check_pa_required_node(state)
    assert result["pa_required"] is True
    assert result["current_node"] == "check_pa_required"
    assert len(result["audit_trail"]) > 0


@pytest.mark.asyncio
async def test_check_pa_required_node_exempt():
    """check_pa_required_node correctly identifies PA is NOT required."""
    state = _make_state(input_data={
        "procedure_code": "99213",  # Office visit — exempt
        "subscriber_id": "MEM-001",
        "subscriber_first_name": "Jane",
        "subscriber_last_name": "Doe",
        "payer_id": "BCBS01",
        "payer_name": "BCBS",
    })
    result = await check_pa_required_node(state)
    assert result["pa_required"] is False


@pytest.mark.asyncio
async def test_check_pa_required_node_missing_procedure():
    """check_pa_required_node sets error for missing procedure code."""
    state = _make_state(input_data={
        "subscriber_id": "MEM-001",
        "subscriber_first_name": "Jane",
        "subscriber_last_name": "Doe",
        "payer_id": "BCBS01",
    })
    result = await check_pa_required_node(state)
    assert result.get("error") is not None
    assert "procedure_code" in result["error"]


@pytest.mark.asyncio
async def test_gather_clinical_docs_node():
    """gather_clinical_docs_node populates clinical evidence in state."""
    state = _make_state()
    state["pa_required"] = True
    result = await gather_clinical_docs_node(state)
    assert result["current_node"] == "gather_clinical_docs"
    assert "clinical_evidence" in result
    assert "clinical_summary" in result
    assert result["clinical_summary"]["conditions_count"] >= 1
    assert result["clinical_summary"]["medications_count"] >= 1


@pytest.mark.asyncio
async def test_build_pa_request_node():
    """build_pa_request_node creates X12 278 and Da Vinci PAS."""
    state = _make_state()
    state["clinical_evidence"] = await gather_clinical_documents(patient_id="PAT-001")
    result = await build_pa_request_node(state)
    assert result["current_node"] == "build_pa_request"
    assert result["x12_278_result"]["success"] is True
    assert result["davinci_pas_result"]["success"] is True


@pytest.mark.asyncio
async def test_determine_submission_channel_node():
    """determine_submission_channel_node defaults to clearinghouse."""
    state = _make_state()
    result = await determine_submission_channel_node(state)
    assert result["submission_channel"] == "clearinghouse"


@pytest.mark.asyncio
async def test_submit_pa_node_clearinghouse():
    """submit_pa_node submits via clearinghouse by default."""
    state = _make_state()
    state["submission_channel"] = "clearinghouse"
    state["x12_278_result"] = {"success": True, "x12_278": "ISA*...", "control_number": "123"}
    result = await submit_pa_node(state)
    assert result["current_node"] == "submit_pa"
    assert result["submission_result"]["success"] is True


@pytest.mark.asyncio
async def test_track_status_node():
    """track_status_node tracks submission status."""
    state = _make_state()
    state["submission_result"] = {
        "success": True,
        "transaction_id": "PA-ABC123",
        "status": "pending",
    }
    result = await track_status_node(state)
    assert result["current_node"] == "track_status"
    assert result["pa_status"] in ("approved", "denied", "pended", "cancelled")


@pytest.mark.asyncio
async def test_handle_denial_node_not_denied():
    """handle_denial_node skips when PA is not denied."""
    state = _make_state()
    state["pa_status"] = "approved"
    result = await handle_denial_node(state)
    assert "denial_info" not in result
    node_actions = [e["action"] for e in result["audit_trail"]]
    assert "denial_handling_skipped" in node_actions


@pytest.mark.asyncio
async def test_handle_denial_node_denied():
    """handle_denial_node sets up denial context and triggers HITL."""
    state = _make_state()
    state["pa_status"] = "denied"
    state["pa_status_result"] = {
        "determination_reason": "Procedure not covered under current plan"
    }
    result = await handle_denial_node(state)
    assert result["needs_review"] is True
    assert "denial_info" in result
    assert "Procedure not covered" in result["denial_info"]["denial_reason"]
    assert "Appeal required" in result["review_reason"]


@pytest.mark.asyncio
async def test_generate_appeal_node_denied():
    """generate_appeal_node creates appeal letter when denied."""
    state = _make_state()
    state["pa_status"] = "denied"
    state["denial_info"] = {
        "denial_reason": "Medical necessity not established",
        "denial_date": "2025-03-15",
        "authorization_number": "AUTH-001",
        "procedure_code": "27447",
        "payer_name": "BCBS",
    }
    state["clinical_evidence"] = await gather_clinical_documents(patient_id="PAT-001")
    result = await generate_appeal_node(state)
    assert result["current_node"] == "generate_appeal"
    assert result["appeal_letter"] != ""
    assert "appeal_result" in result
    assert result["appeal_result"]["success"] is True


@pytest.mark.asyncio
async def test_generate_appeal_node_not_denied():
    """generate_appeal_node skips when not denied."""
    state = _make_state()
    state["pa_status"] = "approved"
    result = await generate_appeal_node(state)
    assert result.get("appeal_letter", "") == ""


@pytest.mark.asyncio
async def test_evaluate_confidence_approved():
    """evaluate_confidence_node gives high confidence for approved PA."""
    state = _make_state()
    state["pa_status"] = "approved"
    state["clinical_summary"] = {"total_documents": 10}
    result = await evaluate_confidence_node(state)
    assert result["confidence"] >= 0.9
    assert result["needs_review"] is False


@pytest.mark.asyncio
async def test_evaluate_confidence_denied_triggers_review():
    """evaluate_confidence_node triggers review for denied PA."""
    state = _make_state()
    state["pa_status"] = "denied"
    state["clinical_summary"] = {"total_documents": 10}
    result = await evaluate_confidence_node(state)
    assert result["needs_review"] is True
    assert result["confidence"] < 0.7
    assert "denied" in result["review_reason"].lower()


@pytest.mark.asyncio
async def test_evaluate_confidence_no_clinical_docs():
    """evaluate_confidence_node flags insufficient clinical evidence."""
    state = _make_state()
    state["pa_status"] = "pended"
    state["clinical_summary"] = {"total_documents": 0}
    result = await evaluate_confidence_node(state)
    assert result["needs_review"] is True
    assert result["confidence"] < 0.7
    assert "documentation" in result["review_reason"].lower()


@pytest.mark.asyncio
async def test_evaluate_confidence_submission_failed():
    """evaluate_confidence_node handles submission failure."""
    state = _make_state()
    state["pa_status"] = "submission_failed"
    state["clinical_summary"] = {"total_documents": 5}
    result = await evaluate_confidence_node(state)
    assert result["needs_review"] is True
    assert result["confidence"] < 0.7


# ── Full Agent Lifecycle Tests ────────────────────────────────────────


@pytest.mark.asyncio
async def test_prior_auth_agent_build_graph():
    """PriorAuthAgent builds a compilable graph with all contract nodes."""
    mock_backend = MockLLMBackend(responses=[
        '{"confidence": 0.85, "decision": {"pa_status": "approved"}, "tool_calls": []}'
    ])
    llm_provider = LLMProvider(primary=mock_backend, phi_safe=False)

    agent = PriorAuthAgent(llm_provider=llm_provider)
    graph = agent.build_graph()

    assert graph is not None
    assert "check_pa_required" in graph.node_names
    assert "gather_clinical_docs" in graph.node_names
    assert "build_pa_request" in graph.node_names
    assert "determine_submission_channel" in graph.node_names
    assert "submit_pa" in graph.node_names
    assert "track_status" in graph.node_names
    assert "handle_denial" in graph.node_names
    assert "generate_appeal" in graph.node_names
    assert "evaluate_confidence" in graph.node_names
    assert "escalate" in graph.node_names
    assert "output" in graph.node_names


@pytest.mark.asyncio
async def test_prior_auth_agent_run_full_cycle():
    """PriorAuthAgent.run() executes full graph with mock LLM."""
    mock_backend = MockLLMBackend(responses=[
        '{"confidence": 0.85, "decision": {"pa_status": "pending"}, "tool_calls": []}'
    ])
    llm_provider = LLMProvider(primary=mock_backend, phi_safe=False)

    agent = PriorAuthAgent(llm_provider=llm_provider)
    state = await agent.run(
        task_id=str(uuid.uuid4()),
        input_data={
            "procedure_code": "27447",
            "diagnosis_codes": ["M17.11"],
            "subscriber_id": "MEM-12345",
            "subscriber_first_name": "John",
            "subscriber_last_name": "Smith",
            "subscriber_dob": "19650315",
            "payer_id": "BCBS01",
            "payer_name": "BCBS",
            "provider_npi": "1234567890",
            "provider_name": "Dr. Johnson",
            "date_of_service": "20250401",
        },
    )

    assert state["agent_type"] == "prior_auth"
    assert len(state.get("audit_trail", [])) > 0

    # Verify it went through the PA-specific pipeline
    node_actions = [e.get("node", "") for e in state.get("audit_trail", [])]
    assert "check_pa_required" in node_actions
    assert "gather_clinical_docs" in node_actions
    assert "build_pa_request" in node_actions
    assert "output" in node_actions

    # PA should be identified as required
    assert state.get("pa_required") is True


@pytest.mark.asyncio
async def test_prior_auth_agent_exempt_procedure_shortcircuits():
    """PriorAuthAgent skips PA pipeline for exempt procedures."""
    mock_backend = MockLLMBackend(responses=[
        '{"confidence": 0.95, "decision": {"pa_required": false}, "tool_calls": []}'
    ])
    llm_provider = LLMProvider(primary=mock_backend, phi_safe=False)

    agent = PriorAuthAgent(llm_provider=llm_provider)
    state = await agent.run(
        task_id=str(uuid.uuid4()),
        input_data={
            "procedure_code": "99213",  # Office visit — exempt
            "subscriber_id": "MEM-001",
            "subscriber_first_name": "Jane",
            "subscriber_last_name": "Doe",
            "payer_id": "BCBS01",
            "payer_name": "BCBS",
            "date_of_service": "20250401",
        },
    )

    assert state.get("pa_required") is False
    # Should go straight to output without gather/build/submit
    node_actions = [e.get("node", "") for e in state.get("audit_trail", [])]
    assert "check_pa_required" in node_actions
    assert "output" in node_actions
    # Should NOT have gone through the full pipeline
    assert "submit_pa" not in node_actions


@pytest.mark.asyncio
async def test_prior_auth_agent_generates_audit_trail():
    """PriorAuthAgent creates complete audit trail entries."""
    mock_backend = MockLLMBackend(responses=[
        '{"confidence": 0.85, "decision": {"pa_status": "approved"}, "tool_calls": []}'
    ])
    llm_provider = LLMProvider(primary=mock_backend, phi_safe=False)

    agent = PriorAuthAgent(llm_provider=llm_provider)
    state = await agent.run(
        task_id=str(uuid.uuid4()),
        input_data={
            "procedure_code": "27447",
            "diagnosis_codes": ["M17.11"],
            "subscriber_id": "MEM-12345",
            "subscriber_first_name": "John",
            "subscriber_last_name": "Smith",
            "payer_id": "BCBS01",
            "payer_name": "BCBS",
            "provider_npi": "1234567890",
            "provider_name": "Dr. Johnson",
            "date_of_service": "20250401",
        },
    )

    audit_trail = state.get("audit_trail", [])
    assert len(audit_trail) >= 5  # At minimum: check, gather, build, channel, submit, track, output

    # Each audit entry has required structure
    for entry in audit_trail:
        assert "timestamp" in entry
        assert "node" in entry
        assert "action" in entry


# ── Tool Definition Tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_prior_auth_tools_definitions():
    """get_prior_auth_tools returns all 8 tool definitions."""
    tools = get_prior_auth_tools()
    assert len(tools) == 8
    names = {t.name for t in tools}
    assert "check_pa_required" in names
    assert "gather_clinical_documents" in names
    assert "build_278_request" in names
    assert "parse_278_response" in names
    assert "submit_pa_to_clearinghouse" in names
    assert "poll_pa_status" in names
    assert "generate_appeal_letter" in names
    assert "build_davinci_pas_request" in names


@pytest.mark.asyncio
async def test_parse_278_response_empty():
    """parse_278_response handles empty response gracefully."""
    result = await parse_278_response("")
    assert "success" in result


# ── Worker Registration Tests ────────────────────────────────────────


def test_generate_post_poll_appeal_registered_in_worker():
    """Critical: generate_post_poll_appeal must be registered as a Temporal activity."""
    from app.workflows.worker import get_registered_activities
    from app.workflows.prior_auth import generate_post_poll_appeal

    activities = get_registered_activities()
    assert generate_post_poll_appeal in activities, (
        "generate_post_poll_appeal is not registered in the Temporal worker. "
        "The denied-after-poll path will fail at runtime."
    )


# ── Portal/Payer-API Fallback Tests ──────────────────────────────────


@pytest.mark.asyncio
async def test_portal_failure_falls_back_to_clearinghouse():
    """When portal submission fails, submit_pa_node should fall back to clearinghouse."""
    from app.agents.prior_auth.graph import submit_pa_node

    state = _make_state(
        submission_channel="portal",
        x12_278_result={"x12_278": "ISA*...", "control_number": "000000001"},
    )

    with (
        patch(
            "app.agents.prior_auth.rpa_stub.submit_via_portal",
            new_callable=AsyncMock,
            return_value={"success": False, "error": "Portal unavailable"},
        ),
        patch(
            "app.agents.prior_auth.tools.submit_pa_to_clearinghouse",
            new_callable=AsyncMock,
            return_value={
                "success": True,
                "transaction_id": "PA-FALLBACK-001",
                "status": "pending",
            },
        ) as mock_ch,
    ):
        result = await submit_pa_node(state)

    # Clearinghouse should have been called as fallback
    mock_ch.assert_called_once()
    assert result["submission_result"]["success"] is True
    assert result["submission_result"]["transaction_id"] == "PA-FALLBACK-001"
    assert result["submission_channel"] == "clearinghouse"


@pytest.mark.asyncio
async def test_payer_api_failure_falls_back_to_clearinghouse():
    """When payer API submission fails, submit_pa_node should fall back to clearinghouse."""
    from app.agents.prior_auth.graph import submit_pa_node

    state = _make_state(
        submission_channel="payer_api",
        x12_278_result={"x12_278": "ISA*...", "control_number": "000000001"},
    )

    with (
        patch(
            "app.agents.prior_auth.tools.submit_pa_via_payer_api",
            new_callable=AsyncMock,
            return_value={"success": False, "error": "Payer API timeout"},
        ),
        patch(
            "app.agents.prior_auth.tools.submit_pa_to_clearinghouse",
            new_callable=AsyncMock,
            return_value={
                "success": True,
                "transaction_id": "PA-FALLBACK-002",
                "status": "pending",
            },
        ) as mock_ch,
    ):
        result = await submit_pa_node(state)

    mock_ch.assert_called_once()
    assert result["submission_result"]["success"] is True
    assert result["submission_channel"] == "clearinghouse"


# ── Patient ID Validation Tests ──────────────────────────────────────


def test_schema_rejects_invalid_patient_id():
    """PriorAuthRequest schema should reject non-UUID patient_id."""
    import pytest as _pytest
    from app.schemas.prior_auth import PriorAuthRequest as PriorAuthRequestSchema

    with _pytest.raises(Exception):  # ValidationError
        PriorAuthRequestSchema(
            procedure_code="27447",
            subscriber_id="MEM-12345",
            subscriber_first_name="John",
            subscriber_last_name="Smith",
            payer_id="BCBS01",
            patient_id="not-a-valid-uuid",
        )


def test_schema_accepts_valid_uuid_patient_id():
    """PriorAuthRequest schema should accept a valid UUID patient_id."""
    from app.schemas.prior_auth import PriorAuthRequest as PriorAuthRequestSchema

    req = PriorAuthRequestSchema(
        procedure_code="27447",
        subscriber_id="MEM-12345",
        subscriber_first_name="John",
        subscriber_last_name="Smith",
        payer_id="BCBS01",
        patient_id=str(uuid.uuid4()),
    )
    assert req.patient_id != ""


def test_schema_rejects_empty_patient_id():
    """PriorAuthRequest schema should reject empty patient_id."""
    import pytest as _pytest
    from app.schemas.prior_auth import PriorAuthRequest as PriorAuthRequestSchema

    with _pytest.raises(Exception):  # ValidationError
        PriorAuthRequestSchema(
            procedure_code="27447",
            subscriber_id="MEM-12345",
            subscriber_first_name="John",
            subscriber_last_name="Smith",
            payer_id="BCBS01",
            patient_id="",
        )


def test_schema_rejects_missing_patient_id():
    """PriorAuthRequest schema should reject missing patient_id."""
    import pytest as _pytest
    from app.schemas.prior_auth import PriorAuthRequest as PriorAuthRequestSchema

    with _pytest.raises(Exception):  # ValidationError
        PriorAuthRequestSchema(
            procedure_code="27447",
            subscriber_id="MEM-12345",
            subscriber_first_name="John",
            subscriber_last_name="Smith",
            payer_id="BCBS01",
        )


def test_schema_rejects_missing_payer_id():
    """PriorAuthRequest schema should reject missing payer_id."""
    import pytest as _pytest
    from app.schemas.prior_auth import PriorAuthRequest as PriorAuthRequestSchema

    with _pytest.raises(Exception):  # ValidationError
        PriorAuthRequestSchema(
            procedure_code="27447",
            subscriber_id="MEM-12345",
            subscriber_first_name="John",
            subscriber_last_name="Smith",
            patient_id=str(uuid.uuid4()),
        )


# ── Cancelled Status Mapping Test ────────────────────────────────────


@pytest.mark.asyncio
async def test_write_prior_auth_result_maps_cancelled_status(db_session, test_engine):
    """write_prior_auth_result should map 'cancelled' pa_status correctly."""
    from app.workflows.prior_auth import write_prior_auth_result, _get_activity_session_factory
    from app.models.prior_auth import PriorAuthRequest as PriorAuthModel
    from app.models.agent_task import AgentTask
    from app.models.organization import Organization
    from app.models.patient import Patient

    # Create supporting records
    org = Organization(name="Cancelled Test Org", npi=f"CT{uuid.uuid4().hex[:8]}", tax_id="99-0000001")
    db_session.add(org)
    await db_session.flush()

    patient = Patient(
        organization_id=org.id,
        mrn=f"MRN-CT-{uuid.uuid4().hex[:6]}",
        first_name="Jane",
        last_name="Doe",
        date_of_birth=date(1980, 1, 1),
        gender="female",
    )
    db_session.add(patient)
    await db_session.flush()

    task = AgentTask(
        agent_type="prior_auth",
        status="running",
        patient_id=patient.id,
        organization_id=org.id,
        input_data={"procedure_code": "27447", "subscriber_id": "MEM-999"},
    )
    db_session.add(task)
    await db_session.flush()

    pa_record = PriorAuthModel(
        task_id=task.id,
        patient_id=patient.id,
        status="pending",
        procedure_code="27447",
        diagnosis_codes=[],
        clinical_info={},
        submission_channel="clearinghouse",
    )
    db_session.add(pa_record)
    await db_session.commit()

    # Patch session factory to use test DB
    class _NDE:
        def __init__(self, e): self._real = e
        def __getattr__(self, n):
            if n == "_real": raise AttributeError
            return getattr(self._real, n)
        async def dispose(self): pass

    safe_engine = _NDE(test_engine)
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS
    factory = async_sessionmaker(safe_engine, class_=AS, expire_on_commit=False)

    with (
        patch("app.workflows.prior_auth._get_activity_session_factory", return_value=(factory, None)),
        patch("app.core.audit.logger.AuditLogger.log", new_callable=AsyncMock),
    ):
        result = await write_prior_auth_result({
            "task_id": str(task.id),
            "agent_decision": {
                "pa_status": "cancelled",
                "confidence": 0.0,
            },
        })

    assert result["success"] is True

    # Verify DB record
    from sqlalchemy import select
    async with factory() as check_session:
        row = (await check_session.execute(
            select(PriorAuthModel).where(PriorAuthModel.task_id == task.id)
        )).scalar_one()
        assert row.status == "cancelled"


# ── Payer Rule Engine Integration Test ───────────────────────────────


@pytest.mark.asyncio
async def test_check_pa_required_with_rule_engine(db_session):
    """check_pa_required integrates with payer rule engine when db_session is provided."""
    from app.models.payer import Payer, PayerRule
    from datetime import date

    # Create a payer with a PA-required rule
    payer = Payer(
        name="Test Payer RE",
        payer_id_code=f"TPRE{uuid.uuid4().hex[:6]}",
        is_active=True,
    )
    db_session.add(payer)
    await db_session.flush()

    # Add a pa_required rule for procedure 99999 (normally "unknown" in static tables)
    rule = PayerRule(
        payer_id=payer.id,
        agent_type="prior_auth",
        rule_type="pa_required",
        description="PA required for custom procedure via rule engine",
        conditions={"procedure_code": {"op": "eq", "value": "99999"}},
        actions={"clinical_docs_needed": ["special_doc"]},
        effective_date=date(2020, 1, 1),
        version=1,
        is_active=True,
    )
    db_session.add(rule)
    await db_session.flush()

    result = await check_pa_required(
        procedure_code="99999",
        payer_id=str(payer.id),
        db_session=db_session,
    )
    assert result["pa_required"] is True
    assert result.get("source") == "rule_engine"
    assert result.get("matched_rule") is not None
    # The rule's actions should flow through
    assert "special_doc" in result.get("clinical_docs_needed", [])


@pytest.mark.asyncio
async def test_check_pa_required_exempt_via_rule_engine(db_session):
    """check_pa_required returns pa_required=False when rule engine has exempt rule."""
    from app.models.payer import Payer, PayerRule
    from datetime import date

    payer = Payer(
        name="Test Payer Exempt RE",
        payer_id_code=f"TPEX{uuid.uuid4().hex[:6]}",
        is_active=True,
    )
    db_session.add(payer)
    await db_session.flush()

    # Add a pa_exempt rule for a procedure that's "required" in static tables
    rule = PayerRule(
        payer_id=payer.id,
        agent_type="prior_auth",
        rule_type="pa_exempt",
        description="PA exempt for 27447 per this payer's policy",
        conditions={"procedure_code": {"op": "eq", "value": "27447"}},
        actions={},
        effective_date=date(2020, 1, 1),
        version=1,
        is_active=True,
    )
    db_session.add(rule)
    await db_session.flush()

    result = await check_pa_required(
        procedure_code="27447",
        payer_id=str(payer.id),
        db_session=db_session,
    )
    assert result["pa_required"] is False
    assert result.get("source") == "rule_engine"
    assert result["reason"] == "exempt_per_payer_rule"


@pytest.mark.asyncio
async def test_check_pa_required_falls_back_to_static_when_no_rules(db_session):
    """When rule engine finds no matching rules, falls back to static tables."""
    from app.models.payer import Payer
    payer = Payer(
        name="No Rules Payer",
        payer_id_code=f"NRP{uuid.uuid4().hex[:6]}",
        is_active=True,
    )
    db_session.add(payer)
    await db_session.flush()

    # No rules in DB — should fall through to static lookup
    result = await check_pa_required(
        procedure_code="27447",  # In static required set
        payer_id=str(payer.id),
        db_session=db_session,
    )
    assert result["pa_required"] is True
    assert result.get("source") == "static"


# ── Regression Tests ─────────────────────────────────────────────────


class TestSubmissionChannelApiNormalization:
    """Regression: submission_channel='api' must be normalized to 'payer_api'."""

    @pytest.mark.asyncio
    async def test_api_channel_normalized_to_payer_api(self):
        """When input specifies submission_channel='api', it must be treated
        as 'payer_api' and not result in submission_failed."""
        state = _make_state(
            input_data={
                "procedure_code": "27447",
                "diagnosis_codes": ["M17.11"],
                "subscriber_id": "MEM-12345",
                "subscriber_first_name": "Jane",
                "subscriber_last_name": "Doe",
                "subscriber_dob": "19700101",
                "payer_id": "BCBS01",
                "payer_name": "Blue Cross",
                "provider_npi": "1234567890",
                "provider_name": "Dr. Smith",
                "patient_id": "PAT-001",
                "date_of_service": "20250401",
                "submission_channel": "api",  # alias for payer_api
            },
        )
        result = await determine_submission_channel_node(state)
        assert result["submission_channel"] == "payer_api"

    @pytest.mark.asyncio
    async def test_payer_api_channel_unchanged(self):
        """Explicit 'payer_api' value is preserved as-is."""
        state = _make_state(
            input_data={
                "procedure_code": "27447",
                "diagnosis_codes": ["M17.11"],
                "subscriber_id": "MEM-12345",
                "subscriber_first_name": "Jane",
                "subscriber_last_name": "Doe",
                "payer_id": "BCBS01",
                "payer_name": "Blue Cross",
                "submission_channel": "payer_api",
            },
        )
        result = await determine_submission_channel_node(state)
        assert result["submission_channel"] == "payer_api"

    @pytest.mark.asyncio
    async def test_unknown_channel_defaults_to_clearinghouse(self):
        """Unknown submission channel falls back to clearinghouse."""
        state = _make_state(
            input_data={
                "procedure_code": "27447",
                "diagnosis_codes": ["M17.11"],
                "subscriber_id": "MEM-12345",
                "subscriber_first_name": "Jane",
                "subscriber_last_name": "Doe",
                "payer_id": "BCBS01",
                "payer_name": "Blue Cross",
                "submission_channel": "carrier_pigeon",
            },
        )
        result = await determine_submission_channel_node(state)
        assert result["submission_channel"] == "clearinghouse"

    @pytest.mark.asyncio
    async def test_api_channel_full_graph_does_not_fail_submission(self):
        """Full agent run with submission_channel='api' should not end in
        submission_failed — the 'api' alias must be normalized before the
        submit_pa node executes."""
        llm = LLMProvider(
            primary=MockLLMBackend(responses=[
                '{"confidence": 0.85, "decision": {}, "tool_calls": []}'
            ]),
            phi_safe=True,
        )
        agent = PriorAuthAgent(llm_provider=llm)
        state = await agent.run(
            task_id=str(uuid.uuid4()),
            input_data={
                "procedure_code": "27447",
                "diagnosis_codes": ["M17.11"],
                "subscriber_id": "MEM-12345",
                "subscriber_first_name": "Jane",
                "subscriber_last_name": "Doe",
                "subscriber_dob": "19700101",
                "payer_id": "BCBS01",
                "payer_name": "Blue Cross",
                "provider_npi": "1234567890",
                "provider_name": "Dr. Smith",
                "patient_id": "PAT-001",
                "date_of_service": "20250401",
                "submission_channel": "api",
            },
        )
        # Submission must not have failed because of channel mismatch
        assert state.get("pa_status") != "submission_failed", (
            f"Expected pa_status != 'submission_failed' but got '{state.get('pa_status')}'"
        )
        assert state.get("submission_channel") in ("payer_api", "clearinghouse")


class TestPaRequiredNodeUsesDbSession:
    """Regression: check_pa_required_node must use payer rule engine via db_session."""

    @pytest.mark.asyncio
    async def test_check_pa_required_node_passes_db_session(self, db_session):
        """When state contains db_session, the node passes it to the tool so
        that payer rules from the database are evaluated."""
        from app.models.payer import Payer, PayerRule

        payer = Payer(
            name="Test Payer",
            payer_id_code=f"TP{uuid.uuid4().hex[:6]}",
            is_active=True,
        )
        db_session.add(payer)
        await db_session.flush()

        # Create an exemption rule for procedure 27447 with this payer
        rule = PayerRule(
            payer_id=payer.id,
            agent_type="prior_auth",
            rule_type="pa_exempt",
            description="Test exemption rule",
            conditions={"procedure_code": {"op": "eq", "value": "27447"}},
            actions={},
            effective_date=date(2020, 1, 1),
            version=1,
            is_active=True,
        )
        db_session.add(rule)
        await db_session.flush()

        state = _make_state(
            input_data={
                "procedure_code": "27447",
                "diagnosis_codes": ["M17.11"],
                "subscriber_id": "MEM-12345",
                "subscriber_first_name": "Jane",
                "subscriber_last_name": "Doe",
                "payer_id": str(payer.id),
                "payer_name": "Test Payer",
            },
            db_session=db_session,
        )

        result = await check_pa_required_node(state)
        # The node should have used the rule engine (via db_session) and
        # found the exemption rule
        assert result["pa_required"] is False
        assert result.get("pa_check_result", {}).get("source") == "rule_engine"
        assert result.get("pa_check_result", {}).get("reason") == "exempt_per_payer_rule"

    @pytest.mark.asyncio
    async def test_check_pa_required_node_without_db_session_uses_static(self):
        """When no db_session in state, falls back to static tables."""
        state = _make_state(
            input_data={
                "procedure_code": "27447",
                "diagnosis_codes": ["M17.11"],
                "subscriber_id": "MEM-12345",
                "subscriber_first_name": "Jane",
                "subscriber_last_name": "Doe",
                "payer_id": "UNKNOWN_PAYER",
                "payer_name": "Unknown",
            },
        )
        # No db_session in state
        result = await check_pa_required_node(state)
        assert result["pa_required"] is True
        assert result.get("pa_check_result", {}).get("source") == "static"


class TestPostPollDenialAppealFlow:
    """Regression: post-poll denial path must include evidence gaps and attachment manifest."""

    @pytest.mark.asyncio
    async def test_generate_post_poll_appeal_includes_evidence_gaps(self):
        """The post-poll appeal activity must return evidence_gaps from the
        denial analysis (not from generate_appeal_letter which doesn't have them)."""
        from app.workflows.prior_auth import generate_post_poll_appeal

        result = await generate_post_poll_appeal({
            "task_id": str(uuid.uuid4()),
            "input_data": {
                "patient_id": "PAT-001",
                "subscriber_first_name": "John",
                "subscriber_last_name": "Smith",
                "subscriber_dob": "19650315",
                "procedure_code": "27447",
                "procedure_description": "Total knee replacement",
                "diagnosis_codes": ["M17.11"],
                "payer_name": "Blue Cross",
                "payer_policy_reference": "",
            },
            "determination_reason": "Medical necessity not established",
            "authorization_number": "AUTH-123",
        })

        assert result["success"] is True
        data = result["data"]

        # Must include evidence_gaps from denial analysis
        assert "evidence_gaps" in data
        assert isinstance(data["evidence_gaps"], list)

        # Must include attachment_manifest from denial analysis
        assert "attachment_manifest" in data
        assert isinstance(data["attachment_manifest"], list)

        # Must include denial_category from the categorization
        assert "denial_category" in data
        assert data["denial_category"] == "medical_necessity"

        # Appeal package must contain both evidence_gaps and attachment_manifest
        package = data.get("appeal_package", {})
        assert "evidence_gaps" in package
        assert "attachment_manifest" in package

        # Appeal letter must be non-empty
        assert len(data.get("appeal_letter", "")) > 100

    @pytest.mark.asyncio
    async def test_generate_post_poll_appeal_insufficient_docs_has_gaps(self):
        """When clinical evidence is limited, evidence gaps should be identified."""
        from app.workflows.prior_auth import generate_post_poll_appeal

        result = await generate_post_poll_appeal({
            "task_id": str(uuid.uuid4()),
            "input_data": {
                "patient_id": "PAT-002",
                "subscriber_first_name": "Jane",
                "subscriber_last_name": "Doe",
                "subscriber_dob": "19800101",
                "procedure_code": "72148",
                "procedure_description": "MRI lumbar spine",
                "diagnosis_codes": ["M54.5"],
                "payer_name": "Aetna",
                "fhir_base_url": "",  # No FHIR → mock data with some evidence
            },
            "determination_reason": "Insufficient documentation provided for review",
            "authorization_number": "",
        })

        data = result["data"]
        # The mock clinical data has conditions, meds, labs, procedures,
        # so some gap categories may still apply. The key assertion is
        # that the evidence_gaps list is populated from _identify_evidence_gaps,
        # not from generate_appeal_letter (which never returned this field).
        assert isinstance(data["evidence_gaps"], list)
        assert isinstance(data["attachment_manifest"], list)
        assert data["denial_category"] == "insufficient_documentation"


# ── Clearinghouse Integration Correctness Tests ──────────────────────


@pytest.mark.asyncio
async def test_poll_pa_status_clearinghouse_calls_check_status_with_string():
    """poll_pa_status must call client.check_status(transaction_id: str),
    not with a TransactionRequest object — regression test for type mismatch bug.
    """
    mock_response = AsyncMock()
    mock_response.parsed_response = {
        "status": "approved",
        "authorization_number": "AUTH-999",
        "effective_date": "2026-01-01",
        "expiration_date": "2026-12-31",
        "determination_reason": "Approved",
    }

    mock_client = AsyncMock()
    mock_client.check_status = AsyncMock(return_value=mock_response)

    with patch(
        "app.core.clearinghouse.factory.get_clearinghouse",
        return_value=mock_client,
    ):
        result = await poll_pa_status(
            transaction_id="PA-TEST-001",
            payer_id="BCBS01",
            clearinghouse_config={
                "clearinghouse_name": "availity",
                "api_endpoint": "https://api.availity.com",
                "credentials": {"key": "val"},
            },
        )

    # Verify check_status was called with a string, not a TransactionRequest
    mock_client.check_status.assert_called_once_with("PA-TEST-001")
    assert result["success"] is True
    assert result["status"] == "approved"
    assert result["authorization_number"] == "AUTH-999"


@pytest.mark.asyncio
async def test_poll_pa_status_clearinghouse_error_returns_failure():
    """When clearinghouse raises ClearinghouseError, poll_pa_status returns
    an error result rather than silently falling through to mock.
    """
    from app.core.clearinghouse.base import ClearinghouseError

    mock_client = AsyncMock()
    mock_client.check_status = AsyncMock(
        side_effect=ClearinghouseError("Service unavailable")
    )

    with patch(
        "app.core.clearinghouse.factory.get_clearinghouse",
        return_value=mock_client,
    ):
        result = await poll_pa_status(
            transaction_id="PA-TEST-002",
            payer_id="BCBS01",
            clearinghouse_config={
                "clearinghouse_name": "availity",
                "api_endpoint": "https://api.availity.com",
            },
        )

    assert result["success"] is False
    assert result["status"] == "error"
    assert "Service unavailable" in result["error"]


@pytest.mark.asyncio
async def test_poll_pa_status_transient_error_returns_failure():
    """Transient network errors return a failure result, not a silent
    fallthrough to mock behaviour.
    """
    mock_client = AsyncMock()
    mock_client.check_status = AsyncMock(
        side_effect=ConnectionError("Connection refused")
    )

    with patch(
        "app.core.clearinghouse.factory.get_clearinghouse",
        return_value=mock_client,
    ):
        result = await poll_pa_status(
            transaction_id="PA-TEST-003",
            payer_id="BCBS01",
            clearinghouse_config={
                "clearinghouse_name": "claim_md",
                "api_endpoint": "https://api.claim.md",
            },
        )

    assert result["success"] is False
    assert result["status"] == "error"
    assert "transient" in result["error"].lower()
