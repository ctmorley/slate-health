"""Unit tests for the Claims & Billing Agent.

Tests cover: code validation (ICD-10, CPT), 837P building, 835 parsing,
denial handling, graph construction and execution, confidence evaluation,
and HITL escalation.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.agents.claims.graph import (
    ClaimsAgent,
    validate_codes_node,
    check_payer_rules_node,
    build_837_node,
    submit_clearinghouse_node,
    track_status_node,
    parse_835_node,
    handle_denial_node,
    evaluate_confidence_node,
    escalate_node,
    output_node,
    run_claims_agent,
)
from app.agents.claims.tools import (
    validate_diagnosis_codes,
    validate_procedure_codes,
    build_837p_claim,
    parse_835_remittance,
    check_claim_status,
    parse_277_response,
    analyze_denial,
    get_claims_tools,
    VALID_ICD10_CODES,
    VALID_CPT_CODES,
)
from app.core.engine.llm_provider import LLMProvider, MockLLMBackend
from app.core.engine.state import create_initial_state


# ── Code Validation Tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_diagnosis_codes_valid():
    """Valid ICD-10 codes pass validation."""
    result = await validate_diagnosis_codes(["J06.9", "I10", "E11.9"])
    assert result["all_valid"] is True
    assert result["valid_count"] == 3
    assert result["invalid_count"] == 0
    for code_result in result["codes"]:
        assert code_result["valid"] is True


@pytest.mark.asyncio
async def test_validate_diagnosis_codes_invalid():
    """Invalid ICD-10 codes are flagged."""
    result = await validate_diagnosis_codes(["J06.9", "INVALID", "123"])
    assert result["all_valid"] is False
    assert result["invalid_count"] == 2
    invalid_codes = [c["code"] for c in result["codes"] if not c["valid"]]
    assert "INVALID" in invalid_codes
    assert "123" in invalid_codes


@pytest.mark.asyncio
async def test_validate_diagnosis_codes_format_check():
    """ICD-10 codes with valid format but not in lookup are flagged for review."""
    result = await validate_diagnosis_codes(["Z99.89"])
    # Format-valid but unknown codes are accepted (valid=True) but require review
    assert result["all_valid"] is False  # Unknown codes mark all_valid=False for HITL
    assert result["codes"][0]["valid"] is True
    assert result["codes"][0]["needs_review"] is True


@pytest.mark.asyncio
async def test_validate_procedure_codes_valid():
    """Valid CPT codes pass validation."""
    result = await validate_procedure_codes(["99213", "36415", "80053"])
    assert result["all_valid"] is True
    assert result["valid_count"] == 3
    assert result["invalid_count"] == 0


@pytest.mark.asyncio
async def test_validate_procedure_codes_invalid():
    """Invalid CPT codes are flagged."""
    result = await validate_procedure_codes(["99213", "ABC", "1234"])
    assert result["all_valid"] is False
    assert result["invalid_count"] == 2


@pytest.mark.asyncio
async def test_validate_procedure_codes_format_check():
    """CPT codes with valid 5-digit format but not in lookup are flagged for review."""
    result = await validate_procedure_codes(["99999"])
    # Format-valid but unknown codes are accepted (valid=True) but require review
    assert result["all_valid"] is False  # Unknown codes mark all_valid=False for HITL
    assert result["codes"][0]["valid"] is True
    assert result["codes"][0]["needs_review"] is True


# ── 837P Building Tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_837p_claim_success():
    """Build 837P claim with valid data."""
    result = await build_837p_claim(
        subscriber_id="INS-12345",
        subscriber_last_name="Doe",
        subscriber_first_name="Jane",
        subscriber_dob="19850615",
        payer_id="BCBS01",
        payer_name="Blue Cross Blue Shield",
        diagnosis_codes=["J06.9"],
        procedure_codes=["99213"],
        total_charge="150.00",
        date_of_service="20260401",
    )
    assert result["success"] is True
    assert "x12_837" in result
    assert result["claim_id"] != ""
    # Verify key X12 segments present
    x12 = result["x12_837"]
    assert "ISA*" in x12
    assert "ST*837*" in x12
    assert "CLM*" in x12
    assert "HI*" in x12
    assert "SV1*" in x12


@pytest.mark.asyncio
async def test_build_837p_claim_all_required_segments():
    """Verify all required 837P segments are present."""
    result = await build_837p_claim(
        subscriber_id="INS-12345",
        subscriber_last_name="Doe",
        subscriber_first_name="Jane",
        payer_id="AETNA01",
        payer_name="Aetna",
        diagnosis_codes=["E11.9", "I10"],
        procedure_codes=["99214"],
        total_charge="200.00",
        date_of_service="20260401",
    )
    assert result["success"] is True
    x12 = result["x12_837"]
    # ISA header
    assert "ISA*" in x12
    # GS header
    assert "GS*HC*" in x12
    # ST transaction header
    assert "ST*837*" in x12
    # BHT
    assert "BHT*" in x12
    # NM1 segments (submitter, receiver, billing, subscriber, payer)
    assert x12.count("NM1*") >= 4
    # CLM claim
    assert "CLM*" in x12
    # HI diagnosis
    assert "HI*ABK:" in x12
    # SV1 service line
    assert "SV1*HC:" in x12
    # DTP date of service
    assert "DTP*472*D8*" in x12
    # SE/GE/IEA closing
    assert "SE*" in x12
    assert "GE*" in x12
    assert "IEA*" in x12


@pytest.mark.asyncio
async def test_build_837p_claim_missing_diagnosis():
    """Build 837P fails without diagnosis codes."""
    result = await build_837p_claim(
        subscriber_id="INS-12345",
        diagnosis_codes=[],
        procedure_codes=["99213"],
    )
    assert result["success"] is False
    assert "diagnosis" in result.get("error", "").lower()


@pytest.mark.asyncio
async def test_build_837p_claim_missing_procedure():
    """Build 837P fails without procedure codes or service lines."""
    result = await build_837p_claim(
        subscriber_id="INS-12345",
        diagnosis_codes=["J06.9"],
    )
    assert result["success"] is False
    assert "service line" in result.get("error", "").lower() or "procedure" in result.get("error", "").lower()


# ── 835 Remittance Parsing Tests ─────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_835_remittance():
    """Parse 835 remittance extracts payment info."""
    raw_835 = (
        "ISA*00*          *00*          *ZZ*SENDER01       *ZZ*RECEIVER01     *260401*0900*^*00501*000000001*0*P*:~\n"
        "GS*HP*SENDER01*RECEIVER01*20260401*0900*000000001*X*005010X221A1~\n"
        "ST*835*0001*005010X221A1~\n"
        "BPR*I*500.00*C*ACH*CCP*01*999999999*DA*123456789**01*999999999*DA*987654321*20260415~\n"
        "TRN*1*CHECK123~\n"
        "N1*PR*Blue Cross Blue Shield*XV*BCBS01~\n"
        "N1*PE*Test Provider*XX*1234567890~\n"
        "CLP*CLM-001*1*500.00*400.00*100.00*MC*PAYER-REF-001~\n"
        "CAS*CO*45*100.00~\n"
        "SVC*HC:99213*150.00*120.00**1~\n"
        "SVC*HC:36415*50.00*40.00**1~\n"
        "SE*10*0001~\n"
        "GE*1*000000001~\n"
        "IEA*1*000000001~\n"
    )
    result = await parse_835_remittance(raw_835)
    assert result["success"] is True
    parsed = result["parsed"]
    assert parsed["transaction_type"] == "835"
    assert parsed["payment"]["amount"] == "500.00"
    assert parsed["payment"]["check_number"] == "CHECK123"
    assert len(parsed["claims"]) == 1
    claim = parsed["claims"][0]
    assert claim["claim_id"] == "CLM-001"
    assert claim["charge_amount"] == "500.00"
    assert claim["paid_amount"] == "400.00"
    assert claim["patient_responsibility"] == "100.00"
    assert len(claim["adjustments"]) == 1
    assert claim["adjustments"][0]["reason_code"] == "45"
    assert len(claim["service_lines"]) == 2


# ── Denial Analysis Tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_analyze_denial_authorization():
    """Denial for missing authorization generates appeal recommendation."""
    result = await analyze_denial(
        denial_code="197",
        denial_reason="Precertification/authorization absent",
        claim_id="CLM-001",
        diagnosis_codes=["J06.9"],
        procedure_codes=["99213"],
        payer_id="BCBS01",
    )
    assert result["category"] == "authorization"
    appeal = result["appeal_recommendation"]
    assert appeal["appealable"] is True
    assert len(appeal["required_docs"]) > 0


@pytest.mark.asyncio
async def test_analyze_denial_coding_error():
    """Denial for coding issue generates correction recommendation."""
    result = await analyze_denial(
        denial_code="4",
        denial_reason="Procedure code inconsistent with modifier",
    )
    assert result["category"] == "modifier"


@pytest.mark.asyncio
async def test_analyze_denial_timely_filing():
    """Timely filing denial has low appeal success likelihood."""
    result = await analyze_denial(
        denial_code="29",
        denial_reason="Timely filing limit exceeded",
    )
    assert result["category"] == "timely_filing"
    assert result["appeal_recommendation"]["success_likelihood"] == "low"


@pytest.mark.asyncio
async def test_analyze_denial_creates_record():
    """Denied claim creates denial record with recommended appeal action."""
    result = await analyze_denial(
        denial_code="197",
        denial_reason="Prior auth required",
        claim_id="CLM-TEST",
    )
    assert result["denial_code"] == "197"
    assert result["claim_id"] == "CLM-TEST"
    assert result["appeal_recommendation"]["appealable"] is True
    assert "strategy" in result["appeal_recommendation"]


# ── Tool Registration Tests ──────────────────────────────────────────


def test_get_claims_tools():
    """Claims tools are properly defined."""
    tools = get_claims_tools()
    assert len(tools) == 8
    tool_names = {t.name for t in tools}
    assert "validate_diagnosis_codes" in tool_names
    assert "validate_procedure_codes" in tool_names
    assert "build_837p_claim" in tool_names
    assert "build_837i_claim" in tool_names
    assert "parse_835_remittance" in tool_names
    assert "check_claim_status" in tool_names
    assert "parse_277_response" in tool_names
    assert "analyze_denial" in tool_names


# ── Graph Node Tests ─────────────────────────────────────────────────


def _make_claims_state(**overrides) -> dict:
    """Create an initial state dict for claims tests."""
    state = create_initial_state(
        task_id=str(uuid.uuid4()),
        agent_type="claims",
        input_data=overrides.pop("input_data", {
            "subscriber_id": "INS-12345",
            "subscriber_first_name": "Jane",
            "subscriber_last_name": "Doe",
            "payer_id": "BCBS01",
            "payer_name": "Blue Cross Blue Shield",
            "diagnosis_codes": ["J06.9", "I10"],
            "procedure_codes": ["99213"],
            "total_charge": "150.00",
            "date_of_service": "20260401",
        }),
    )
    state.update(overrides)
    return dict(state)


@pytest.mark.asyncio
async def test_validate_codes_node_valid():
    """validate_codes_node passes with valid codes."""
    state = _make_claims_state()
    result = await validate_codes_node(state)
    assert result.get("error") is None
    assert result["dx_validation"]["all_valid"] is True
    assert result["cpt_validation"]["all_valid"] is True


@pytest.mark.asyncio
async def test_validate_codes_node_invalid_icd10():
    """validate_codes_node flags invalid ICD-10 codes."""
    state = _make_claims_state(input_data={
        "subscriber_id": "INS-12345",
        "diagnosis_codes": ["INVALID"],
        "procedure_codes": ["99213"],
    })
    result = await validate_codes_node(state)
    assert result["needs_review"] is True
    assert result["confidence"] == 0.4


@pytest.mark.asyncio
async def test_validate_codes_node_missing_diagnosis():
    """validate_codes_node flags missing diagnosis codes for HITL review."""
    state = _make_claims_state(input_data={
        "subscriber_id": "INS-12345",
        "diagnosis_codes": [],
        "procedure_codes": ["99213"],
    })
    result = await validate_codes_node(state)
    assert result.get("needs_review") is True
    assert result.get("confidence") == 0.0
    assert "diagnosis" in result.get("review_reason", "").lower()


@pytest.mark.asyncio
async def test_validate_codes_node_missing_procedure():
    """validate_codes_node flags missing procedure codes for HITL review."""
    state = _make_claims_state(input_data={
        "subscriber_id": "INS-12345",
        "diagnosis_codes": ["J06.9"],
        "procedure_codes": [],
    })
    result = await validate_codes_node(state)
    assert result.get("needs_review") is True
    assert result.get("confidence") == 0.0
    assert "procedure" in result.get("review_reason", "").lower()


@pytest.mark.asyncio
async def test_build_837_node():
    """build_837_node constructs 837P from input data."""
    state = _make_claims_state()
    state["dx_validation"] = {"all_valid": True}
    state["cpt_validation"] = {"all_valid": True}
    result = await build_837_node(state)
    assert "x12_837_data" in result
    assert result["x12_837_data"]["success"] is True
    assert result["x12_837_data"]["claim_id"] != ""


@pytest.mark.asyncio
async def test_submit_clearinghouse_node():
    """submit_clearinghouse_node prepares submission metadata."""
    state = _make_claims_state()
    state["x12_837_data"] = {"claim_id": "CLM-001", "control_number": "123456789"}
    result = await submit_clearinghouse_node(state)
    assert result["submission_status"] == "submitted"
    assert result["submission_metadata"]["submitted"] is True


@pytest.mark.asyncio
async def test_handle_denial_node_with_denials():
    """handle_denial_node processes detected denials."""
    state = _make_claims_state()
    state["denials_detected"] = [
        {
            "claim_id": "CLM-001",
            "status_code": "4",
            "adjustments": [{"reason_code": "197", "amount": "100.00"}],
        }
    ]
    result = await handle_denial_node(state)
    assert len(result["denial_analyses"]) == 1
    assert result["needs_review"] is True


@pytest.mark.asyncio
async def test_handle_denial_node_no_denials():
    """handle_denial_node skips when no denials detected."""
    state = _make_claims_state()
    result = await handle_denial_node(state)
    assert result.get("denial_analyses") is None or len(result.get("denial_analyses", [])) == 0


@pytest.mark.asyncio
async def test_evaluate_confidence_node_high():
    """High confidence for clean claim."""
    state = _make_claims_state()
    state["dx_validation"] = {"all_valid": True, "invalid_count": 0}
    state["cpt_validation"] = {"all_valid": True, "invalid_count": 0}
    result = await evaluate_confidence_node(state)
    assert result["confidence"] >= 0.7
    assert result["needs_review"] is False


@pytest.mark.asyncio
async def test_evaluate_confidence_node_invalid_codes():
    """Low confidence for invalid codes."""
    state = _make_claims_state()
    state["dx_validation"] = {"all_valid": False, "invalid_count": 1}
    state["cpt_validation"] = {"all_valid": True, "invalid_count": 0}
    result = await evaluate_confidence_node(state)
    assert result["confidence"] < 0.7
    assert result["needs_review"] is True


@pytest.mark.asyncio
async def test_evaluate_confidence_node_high_value_claim():
    """High-value claims trigger review."""
    state = _make_claims_state(input_data={
        "subscriber_id": "INS-12345",
        "diagnosis_codes": ["J06.9"],
        "procedure_codes": ["99213"],
        "total_charge": "25000.00",
    })
    state["dx_validation"] = {"all_valid": True, "invalid_count": 0}
    state["cpt_validation"] = {"all_valid": True, "invalid_count": 0}
    result = await evaluate_confidence_node(state)
    assert result["needs_review"] is True
    assert "high-value" in result["review_reason"].lower()


@pytest.mark.asyncio
async def test_evaluate_confidence_node_denials():
    """Denials trigger review."""
    state = _make_claims_state()
    state["dx_validation"] = {"all_valid": True, "invalid_count": 0}
    state["cpt_validation"] = {"all_valid": True, "invalid_count": 0}
    state["denials_detected"] = [{"claim_id": "CLM-001"}]
    result = await evaluate_confidence_node(state)
    assert result["needs_review"] is True
    assert result["confidence"] <= 0.5


# ── Full Agent Run Tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claims_agent_graph_construction():
    """ClaimsAgent builds a valid graph."""
    llm_provider = LLMProvider(primary=MockLLMBackend())
    agent = ClaimsAgent(llm_provider=llm_provider)
    graph = agent.build_graph()
    assert "validate_codes" in graph.node_names
    assert "check_payer_rules" in graph.node_names
    assert "build_837" in graph.node_names
    assert "submit_clearinghouse" in graph.node_names
    assert "track_status" in graph.node_names
    assert "parse_835" in graph.node_names
    assert "handle_denial" in graph.node_names
    assert "evaluate_confidence" in graph.node_names
    assert "output" in graph.node_names


@pytest.mark.asyncio
async def test_claims_agent_full_run_valid():
    """Full claims agent run with valid codes produces claim submission."""
    llm_provider = LLMProvider(primary=MockLLMBackend())
    state = await run_claims_agent(
        input_data={
            "subscriber_id": "INS-12345",
            "subscriber_first_name": "Jane",
            "subscriber_last_name": "Doe",
            "payer_id": "BCBS01",
            "payer_name": "Blue Cross Blue Shield",
            "diagnosis_codes": ["J06.9", "I10"],
            "procedure_codes": ["99213"],
            "total_charge": "150.00",
            "date_of_service": "20260401",
        },
        llm_provider=llm_provider,
    )
    assert state.get("error") is None
    decision = state.get("decision", {})
    assert decision.get("submission_status") == "submitted"
    assert decision.get("claim_id") != ""


@pytest.mark.asyncio
async def test_claims_agent_invalid_codes_hitl():
    """Claims agent with invalid codes escalates to HITL."""
    llm_provider = LLMProvider(primary=MockLLMBackend())
    state = await run_claims_agent(
        input_data={
            "subscriber_id": "INS-12345",
            "diagnosis_codes": ["INVALID_CODE"],
            "procedure_codes": ["99213"],
        },
        llm_provider=llm_provider,
    )
    assert state.get("needs_review") is True
    assert state.get("confidence", 1.0) < 0.7


@pytest.mark.asyncio
async def test_claims_agent_missing_required_fields():
    """Claims agent escalates to HITL on missing diagnosis codes."""
    llm_provider = LLMProvider(primary=MockLLMBackend())
    state = await run_claims_agent(
        input_data={
            "subscriber_id": "INS-12345",
            "diagnosis_codes": [],
            "procedure_codes": ["99213"],
        },
        llm_provider=llm_provider,
    )
    # Missing codes now trigger HITL escalation instead of hard error
    assert state.get("needs_review") is True
    assert state.get("confidence", 1.0) < 0.7
    assert "diagnosis" in state.get("review_reason", "").lower()


@pytest.mark.asyncio
async def test_claims_agent_audit_trail():
    """Claims agent produces audit trail entries."""
    llm_provider = LLMProvider(primary=MockLLMBackend())
    state = await run_claims_agent(
        input_data={
            "subscriber_id": "INS-12345",
            "diagnosis_codes": ["J06.9"],
            "procedure_codes": ["99213"],
            "total_charge": "100.00",
            "date_of_service": "20260401",
        },
        llm_provider=llm_provider,
    )
    audit_trail = state.get("audit_trail", [])
    assert len(audit_trail) > 0
    actions = [entry.get("action", "") for entry in audit_trail]
    assert "codes_validated" in actions
    assert "execution_completed" in actions


@pytest.mark.asyncio
async def test_claims_agent_end_to_end_with_837():
    """End-to-end: encounter → code validation → 837 → submission."""
    llm_provider = LLMProvider(primary=MockLLMBackend())
    state = await run_claims_agent(
        input_data={
            "subscriber_id": "INS-12345",
            "subscriber_first_name": "John",
            "subscriber_last_name": "Smith",
            "subscriber_dob": "19800101",
            "subscriber_gender": "M",
            "payer_id": "AETNA01",
            "payer_name": "Aetna",
            "billing_provider_npi": "9876543210",
            "billing_provider_name": "Metro Health Clinic",
            "billing_provider_tax_id": "987654321",
            "diagnosis_codes": ["E11.9", "I10"],
            "procedure_codes": ["99214", "80053"],
            "total_charge": "350.00",
            "date_of_service": "20260401",
            "place_of_service": "11",
        },
        llm_provider=llm_provider,
    )
    assert state.get("error") is None
    decision = state["decision"]
    assert decision.get("submission_status") == "submitted"
    assert decision.get("dx_validation", {}).get("all_valid") is True
    assert decision.get("cpt_validation", {}).get("all_valid") is True


# ── LLM Prompt Integration Tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_claims_agent_llm_code_validation_reasoning():
    """When LLM provider is configured, validate_codes populates code_validation_reasoning."""
    mock_backend = MockLLMBackend(responses=[
        "Code combination E11.9 + 99214 is appropriate for a diabetes management visit.",
    ])
    llm_provider = LLMProvider(primary=mock_backend)
    state = await run_claims_agent(
        input_data={
            "subscriber_id": "INS-12345",
            "diagnosis_codes": ["E11.9"],
            "procedure_codes": ["99214"],
            "date_of_service": "20260401",
            "payer_id": "BCBS01",
        },
        llm_provider=llm_provider,
    )
    assert state.get("error") is None
    # LLM provider should have been called (at least for code validation)
    assert len(mock_backend.call_history) >= 1
    # The code_validation_reasoning field should be populated from LLM response
    assert state.get("code_validation_reasoning") is not None
    assert "appropriate" in state["code_validation_reasoning"].lower() or len(state["code_validation_reasoning"]) > 0


@pytest.mark.asyncio
async def test_claims_agent_llm_denial_reasoning():
    """When LLM provider is configured and denials exist, denial_reasoning is populated."""
    mock_backend = MockLLMBackend(responses=[
        "Code validation looks good.",
        "The denial for CO-4 (procedure code inconsistent with modifier) suggests a modifier is needed.",
    ])
    llm_provider = LLMProvider(primary=mock_backend)

    # Build state manually to inject a denial scenario through the graph
    state = create_initial_state(
        task_id=str(uuid.uuid4()),
        agent_type="claims",
        input_data={
            "subscriber_id": "INS-12345",
            "diagnosis_codes": ["J06.9"],
            "procedure_codes": ["99213"],
            "payer_id": "BCBS01",
            "date_of_service": "20260401",
            "total_charge": "150.00",
        },
    )
    state["_llm_provider"] = llm_provider

    # Run validate_codes to populate code fields
    state = await validate_codes_node(state)

    # Simulate a denial scenario by setting up parse_835 output
    state["denials_detected"] = [
        {
            "claim_id": "CLM-001",
            "status_code": "4",
            "denial_reason": "Procedure code inconsistent with modifier",
            "adjustments": [{"reason_code": "CO-4", "amount": "150.00"}],
        },
    ]

    # Run handle_denial with LLM provider in state
    state = await handle_denial_node(state)

    assert state.get("denial_analyses") is not None
    assert len(state["denial_analyses"]) == 1
    # LLM should have been called for denial analysis
    assert state.get("denial_reasoning") is not None
    assert len(state["denial_reasoning"]) > 0


@pytest.mark.asyncio
async def test_claims_agent_llm_provider_injected_via_run():
    """ClaimsAgent.run() injects _llm_provider into state for graph nodes."""
    mock_backend = MockLLMBackend(responses=["LLM augmented response"])
    llm_provider = LLMProvider(primary=mock_backend)
    agent = ClaimsAgent(llm_provider=llm_provider)

    state = await agent.run(
        input_data={
            "subscriber_id": "INS-12345",
            "diagnosis_codes": ["J06.9"],
            "procedure_codes": ["99213"],
            "date_of_service": "20260401",
        },
    )

    assert state.get("error") is None
    # Verify the LLM was actually called (proving _llm_provider was injected)
    assert len(mock_backend.call_history) >= 1


# ── Temporal-path Missing Code Escalation Tests ─────────────────────


@pytest.mark.asyncio
async def test_validate_claims_input_missing_codes_passes_to_agent():
    """Temporal validate_claims_input allows missing codes through for agent HITL review."""
    from app.workflows.claims import validate_claims_input

    # Missing diagnosis codes — should succeed with warning, not hard-fail
    result = await validate_claims_input({
        "task_id": "test-task-1",
        "input_data": {
            "subscriber_id": "INS-12345",
            "procedure_codes": ["99213"],
            # diagnosis_codes intentionally missing
        },
    })
    assert result["success"] is True, (
        f"Missing diagnosis codes should pass validation (routed to agent HITL), got error: {result.get('error')}"
    )
    assert "warnings" in result.get("data", {})

    # Missing procedure codes — should succeed with warning, not hard-fail
    result2 = await validate_claims_input({
        "task_id": "test-task-2",
        "input_data": {
            "subscriber_id": "INS-12345",
            "diagnosis_codes": ["J06.9"],
            # procedure_codes intentionally missing
        },
    })
    assert result2["success"] is True, (
        f"Missing procedure codes should pass validation (routed to agent HITL), got error: {result2.get('error')}"
    )

    # Missing subscriber_id — should still hard-fail
    result3 = await validate_claims_input({
        "task_id": "test-task-3",
        "input_data": {
            "diagnosis_codes": ["J06.9"],
            "procedure_codes": ["99213"],
        },
    })
    assert result3["success"] is False
    assert "subscriber_id" in result3.get("error", "")


# ── Format-valid unknown code review reason tests ────────────────────


@pytest.mark.asyncio
async def test_validate_codes_node_format_valid_unknown_codes_have_review_reason():
    """validate_codes_node produces a non-empty review_reason when codes are
    format-valid but not in the local lookup (needs_review=True, valid=True).

    Regression test: previously, review_reason was built only from codes
    where valid==False, so format-valid unknown codes could produce an
    empty review_reason despite triggering HITL escalation.
    """
    state = _make_claims_state(input_data={
        "subscriber_id": "INS-12345",
        "diagnosis_codes": ["Z99.89"],  # format-valid, not in local lookup
        "procedure_codes": ["99999"],   # format-valid, not in local lookup
    })
    result = await validate_codes_node(state)
    assert result["needs_review"] is True
    review_reason = result.get("review_reason", "")
    assert review_reason != "", (
        "review_reason must not be empty when format-valid unknown codes trigger review"
    )
    # Should mention verification is needed
    assert "verification" in review_reason.lower() or "verify" in review_reason.lower(), (
        f"review_reason should indicate codes need verification, got: {review_reason}"
    )


@pytest.mark.asyncio
async def test_validate_codes_node_mixed_invalid_and_unknown_codes():
    """review_reason includes both truly invalid and format-valid unknown codes."""
    state = _make_claims_state(input_data={
        "subscriber_id": "INS-12345",
        "diagnosis_codes": ["INVALID", "Z99.89"],  # one invalid format, one unknown
        "procedure_codes": ["99213"],               # known valid
    })
    result = await validate_codes_node(state)
    assert result["needs_review"] is True
    review_reason = result.get("review_reason", "")
    assert "INVALID" in review_reason, "Should mention the invalid-format code"
    assert "Z99.89" in review_reason, "Should mention the unknown-but-format-valid code"


# ── parse_835_node raw_835 parsing Tests ─────────────────────────────


@pytest.mark.asyncio
async def test_parse_835_node_parses_raw_835_from_input():
    """parse_835_node should parse raw_835 from input_data when no tool_results exist."""
    raw_835 = (
        "ISA*00*          *00*          *ZZ*SENDER01       *ZZ*RECEIVER01     "
        "*260401*0900*^*00501*000000001*0*P*:~\n"
        "GS*HP*SENDER01*RECEIVER01*20260401*0900*000000001*X*005010X221A1~\n"
        "ST*835*0001*005010X221A1~\n"
        "BPR*I*500.00*C*ACH*CCP*01*999999999*DA*123456789**01*999999999"
        "*DA*987654321*20260415~\n"
        "TRN*1*CHECK123~\n"
        "N1*PR*Blue Cross Blue Shield*XV*BCBS01~\n"
        "N1*PE*Test Provider*XX*1234567890~\n"
        "CLP*CLM-001*1*500.00*400.00*100.00*MC*PAYER-REF-001~\n"
        "CAS*CO*45*100.00~\n"
        "SVC*HC:99213*150.00*120.00**1~\n"
        "SVC*HC:36415*50.00*40.00**1~\n"
        "SE*10*0001~\n"
        "GE*1*000000001~\n"
        "IEA*1*000000001~\n"
    )
    state = _make_claims_state(input_data={
        "subscriber_id": "INS-12345",
        "diagnosis_codes": ["J06.9"],
        "procedure_codes": ["99213"],
        "raw_835": raw_835,
    })
    # Ensure no pre-parsed remittance or tool_results
    state["tool_results"] = []

    result = await parse_835_node(state)

    assert result.get("remittance_data"), "Should have parsed remittance_data from raw_835"
    assert result["remittance_data"].get("transaction_type") == "835"
    assert result["remittance_data"]["payment"]["amount"] == "500.00"
    # payment_info should be populated
    assert result.get("payment_info"), "Should have extracted payment_info"
    assert result["payment_info"]["total_paid"] == "500.00"


@pytest.mark.asyncio
async def test_parse_835_node_no_raw_835_falls_back_to_remittance_data():
    """Without raw_835 or tool_results, parse_835_node uses remittance_data from input."""
    state = _make_claims_state(input_data={
        "subscriber_id": "INS-12345",
        "diagnosis_codes": ["J06.9"],
        "procedure_codes": ["99213"],
        "remittance_data": {
            "payment": {"amount": "200.00", "method": "ACH", "date": "20260401"},
            "claims": [{"claim_id": "C1", "paid_amount": "200.00", "status_code": "1"}],
        },
    })
    state["tool_results"] = []

    result = await parse_835_node(state)

    assert result.get("remittance_data"), "Should fall back to remittance_data from input"
    assert result["payment_info"]["total_paid"] == "200.00"


# ── poll_claim_status staged transitions Tests ─────────────────────


@pytest.mark.asyncio
async def test_poll_claim_status_reaches_terminal():
    """poll_claim_status should progress through stages and reach terminal."""
    from app.workflows.claims import poll_claim_status

    # Poll attempt 0 → pending (non-terminal)
    result0 = await poll_claim_status({
        "task_id": "T1",
        "claim_id": "C1",
        "input_data": {},
        "poll_attempt": 0,
    })
    assert result0["success"] is True
    assert result0["data"]["terminal"] is False
    assert result0["data"]["status"] == "pending"

    # Poll attempt 1 → in_review (non-terminal)
    result1 = await poll_claim_status({
        "task_id": "T1",
        "claim_id": "C1",
        "input_data": {},
        "poll_attempt": 1,
    })
    assert result1["success"] is True
    assert result1["data"]["terminal"] is False
    assert result1["data"]["status"] == "in_review"

    # Poll attempt 2 → finalized (terminal)
    result2 = await poll_claim_status({
        "task_id": "T1",
        "claim_id": "C1",
        "input_data": {},
        "poll_attempt": 2,
    })
    assert result2["success"] is True
    assert result2["data"]["terminal"] is True
    assert result2["data"]["status"] == "finalized"


@pytest.mark.asyncio
async def test_poll_claim_status_clamped_beyond_stages():
    """poll_attempt beyond max stages stays at terminal."""
    from app.workflows.claims import poll_claim_status

    result = await poll_claim_status({
        "task_id": "T1",
        "claim_id": "C1",
        "input_data": {},
        "poll_attempt": 99,
    })
    assert result["success"] is True
    assert result["data"]["terminal"] is True


# ── Clearinghouse Config Wiring Tests ────────────────────────────────


@pytest.mark.asyncio
async def test_submit_claim_uses_clearinghouse_config():
    """submit_claim_to_clearinghouse uses the provided config
    instead of hardcoding mock.
    """
    from unittest.mock import AsyncMock, patch, MagicMock
    from app.workflows.claims import submit_claim_to_clearinghouse

    mock_response = MagicMock()
    mock_response.transaction_id = "TXN-123"
    mock_response.status = MagicMock()
    mock_response.status.value = "submitted"

    mock_client = AsyncMock()
    mock_client.submit_transaction = AsyncMock(return_value=mock_response)

    with patch(
        "app.core.clearinghouse.factory.get_clearinghouse",
        return_value=mock_client,
    ) as mock_factory:
        result = await submit_claim_to_clearinghouse({
            "task_id": "T1",
            "x12_data": {"x12_837": "ISA*..."},
            "payer_id": "BCBS01",
            "claim_type": "837P",
            "clearinghouse_config": {
                "clearinghouse_name": "availity",
                "api_endpoint": "https://api.availity.com",
                "credentials": {"key": "val"},
            },
        })

    # Verify factory was called with the provided config, not "mock"
    mock_factory.assert_called_once_with(
        clearinghouse_name="availity",
        api_endpoint="https://api.availity.com",
        credentials={"key": "val"},
    )
    assert result["success"] is True
    assert result["data"]["transaction_id"] == "TXN-123"


@pytest.mark.asyncio
async def test_submit_claim_warns_and_falls_back_without_config():
    """When no clearinghouse_config is provided, the activity logs a
    warning and falls back to mock (for test/dev environments).
    """
    from unittest.mock import patch
    from app.workflows.claims import submit_claim_to_clearinghouse

    # Verify a warning is logged when no clearinghouse_config is provided
    with patch(
        "app.workflows.claims.logger",
    ) as mock_logger:
        result = await submit_claim_to_clearinghouse({
            "task_id": "T1",
            "x12_data": {"x12_837": "ISA*..."},
            "payer_id": "BCBS01",
            "claim_type": "837P",
            # No clearinghouse_config
        })

    # Should still succeed using mock fallback
    assert result["success"] is True
    # Should have logged a warning about missing config
    mock_logger.warning.assert_called_once()
    assert "mock" in mock_logger.warning.call_args[0][0].lower()
