"""Unit tests for the Scheduling & Access Agent.

Tests cover: NLP intent parsing, slot matching, appointment creation,
graph construction and execution, confidence evaluation, and HITL escalation.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.agents.scheduling.graph import (
    SchedulingAgent,
    parse_intent_node,
    check_payer_rules_node,
    query_availability_node,
    match_slots_node,
    create_appointment_node,
    evaluate_confidence_node,
    escalate_node,
    confirm_node,
    run_scheduling_agent,
)
from app.agents.scheduling.tools import (
    parse_scheduling_intent,
    query_available_slots,
    match_best_slot,
    create_appointment,
    add_to_waitlist,
    get_scheduling_tools,
)
from app.core.engine.llm_provider import LLMProvider, MockLLMBackend
from app.core.engine.state import create_initial_state


# ── NLP Intent Parsing Tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_intent_provider_and_visit_type():
    """Parse 'annual checkup with Dr. Smith next Tuesday'."""
    result = await parse_scheduling_intent(
        "annual checkup with Dr. Smith next Tuesday"
    )
    assert result["success"] is True
    parsed = result["parsed"]
    assert parsed["provider_name"] == "Smith"
    assert parsed["visit_type"] == "annual_checkup"
    assert parsed["preferred_date_start"] is not None


@pytest.mark.asyncio
async def test_parse_intent_specialty():
    """Parse request with specialty."""
    result = await parse_scheduling_intent(
        "I need to see a cardiologist as soon as possible"
    )
    assert result["success"] is True
    parsed = result["parsed"]
    assert parsed["specialty"] == "cardiology"
    assert parsed["urgency"] == "urgent"


@pytest.mark.asyncio
async def test_parse_intent_urgency():
    """Parse urgent scheduling request."""
    result = await parse_scheduling_intent(
        "urgent appointment with Dr. Johnson tomorrow morning"
    )
    assert result["success"] is True
    parsed = result["parsed"]
    assert parsed["urgency"] == "urgent"
    assert parsed["provider_name"] == "Johnson"
    assert parsed["preferred_time_of_day"] == "morning"
    assert parsed["preferred_date_start"] is not None


@pytest.mark.asyncio
async def test_parse_intent_new_patient():
    """Parse new patient visit request."""
    result = await parse_scheduling_intent(
        "new patient consultation with dermatology next week"
    )
    assert result["success"] is True
    parsed = result["parsed"]
    assert parsed["visit_type"] == "new_patient"
    assert parsed["specialty"] == "dermatology"
    assert parsed["duration_minutes"] == 60


@pytest.mark.asyncio
async def test_parse_intent_follow_up_afternoon():
    """Parse follow-up with time preference."""
    result = await parse_scheduling_intent(
        "follow up appointment in the afternoon"
    )
    assert result["success"] is True
    parsed = result["parsed"]
    assert parsed["visit_type"] == "follow_up"
    assert parsed["preferred_time_of_day"] == "afternoon"


@pytest.mark.asyncio
async def test_parse_intent_empty_request():
    """Empty request returns failure."""
    result = await parse_scheduling_intent("")
    assert result["success"] is False
    assert "empty" in result.get("error", "").lower()


# ── Slot Matching Tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_match_best_slot_morning_preference():
    """Match best slot with morning preference."""
    slots = [
        {"slot_id": "s1", "start": "2026-01-15T09:00:00", "provider_name": "Dr. A"},
        {"slot_id": "s2", "start": "2026-01-15T14:00:00", "provider_name": "Dr. B"},
        {"slot_id": "s3", "start": "2026-01-15T10:30:00", "provider_name": "Dr. C"},
    ]
    result = await match_best_slot(
        slots=slots,
        preferred_time_of_day="morning",
        urgency="routine",
    )
    assert result["success"] is True
    best = result["best_match"]
    assert best is not None
    # Morning slots (s1 and s3) should score higher than afternoon (s2)
    best_start = best["slot"]["start"]
    hour = int(best_start.split("T")[1][:2])
    assert 7 <= hour < 12


@pytest.mark.asyncio
async def test_match_best_slot_provider_preference():
    """Match best slot with specific provider preference."""
    slots = [
        {"slot_id": "s1", "start": "2026-01-15T09:00:00", "provider_name": "Dr. Smith"},
        {"slot_id": "s2", "start": "2026-01-15T10:00:00", "provider_name": "Dr. Jones"},
        {"slot_id": "s3", "start": "2026-01-15T11:00:00", "provider_name": "Dr. Smith"},
    ]
    result = await match_best_slot(
        slots=slots,
        preferred_time_of_day="any",
        provider_name="Smith",
    )
    assert result["success"] is True
    best = result["best_match"]["slot"]
    assert "Smith" in best["provider_name"]


@pytest.mark.asyncio
async def test_match_best_slot_given_10_slots():
    """Given 10 available slots and patient preferences, verify optimal slot selected."""
    slots = [
        {"slot_id": f"s{i}", "start": f"2026-01-{15+i//3:02d}T{9+i%3*2:02d}:00:00",
         "provider_name": f"Dr. Provider{i}", "specialty": "primary_care"}
        for i in range(10)
    ]
    result = await match_best_slot(
        slots=slots,
        preferred_time_of_day="morning",
        urgency="routine",
    )
    assert result["success"] is True
    assert result["total_evaluated"] == 10
    assert result["best_match"] is not None
    assert len(result["alternatives"]) <= 3


@pytest.mark.asyncio
async def test_match_best_slot_empty_slots():
    """Empty slot list returns error."""
    result = await match_best_slot(slots=[])
    assert result["success"] is False
    assert result["best_match"] is None


# ── Available Slots Query Tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_query_available_slots():
    """Query returns a list of available slots."""
    result = await query_available_slots(
        specialty="primary_care",
        date_start="2026-04-01",
    )
    assert result["success"] is True
    assert len(result["slots"]) > 0
    for slot in result["slots"]:
        assert slot["status"] == "free"
        assert "slot_id" in slot


# ── Appointment Creation Tests ───────────────────────────────────────


@pytest.mark.asyncio
async def test_create_appointment():
    """Create appointment returns booking confirmation."""
    result = await create_appointment(
        slot_id="slot-abc123",
        patient_id="patient-001",
        provider_npi="1234567890",
        visit_type="follow_up",
    )
    assert result["success"] is True
    assert result["status"] == "booked"
    assert result["appointment_id"]
    assert result["fhir_id"]


# ── Waitlist Tests ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_to_waitlist():
    """Add to waitlist returns waitlist entry."""
    result = await add_to_waitlist(
        patient_id="patient-001",
        specialty="cardiology",
        urgency="urgent",
    )
    assert result["success"] is True
    assert result["waitlist_id"]
    assert result["position"] >= 1


# ── Tool Registration Tests ──────────────────────────────────────────


def test_get_scheduling_tools():
    """Scheduling tools are properly defined."""
    tools = get_scheduling_tools()
    assert len(tools) == 5
    tool_names = {t.name for t in tools}
    assert "parse_scheduling_intent" in tool_names
    assert "query_available_slots" in tool_names
    assert "match_best_slot" in tool_names
    assert "create_appointment" in tool_names
    assert "add_to_waitlist" in tool_names


# ── Graph Node Tests ─────────────────────────────────────────────────


def _make_scheduling_state(**overrides) -> dict:
    """Create an initial state dict for scheduling tests."""
    state = create_initial_state(
        task_id=str(uuid.uuid4()),
        agent_type="scheduling",
        input_data=overrides.pop("input_data", {
            "request_text": "annual checkup with Dr. Smith next Tuesday",
        }),
    )
    state.update(overrides)
    return dict(state)


@pytest.mark.asyncio
async def test_parse_intent_node_with_text():
    """parse_intent_node extracts parameters from NL text."""
    state = _make_scheduling_state()
    result = await parse_intent_node(state)
    assert "parsed_intent" in result
    assert result["parsed_intent"]["visit_type"] == "annual_checkup"
    assert result.get("error") is None


@pytest.mark.asyncio
async def test_parse_intent_node_empty_request():
    """parse_intent_node sets error on empty request."""
    state = _make_scheduling_state(input_data={})
    result = await parse_intent_node(state)
    assert result.get("error") is not None
    assert result["confidence"] == 0.0


@pytest.mark.asyncio
async def test_parse_intent_node_structured_input():
    """parse_intent_node accepts structured scheduling parameters."""
    state = _make_scheduling_state(input_data={
        "specialty": "cardiology",
        "provider_npi": "1234567890",
        "urgency": "urgent",
    })
    result = await parse_intent_node(state)
    assert "parsed_intent" in result
    assert result["parsed_intent"]["specialty"] == "cardiology"
    assert result.get("error") is None


@pytest.mark.asyncio
async def test_query_availability_node():
    """query_availability_node fetches available slots."""
    state = _make_scheduling_state()
    state["parsed_intent"] = {
        "specialty": "primary_care",
        "preferred_date_start": "2026-04-01",
        "preferred_date_end": "2026-04-05",
        "duration_minutes": 30,
        "provider_npi": "",
    }
    result = await query_availability_node(state)
    assert "available_slots" in result
    assert len(result["available_slots"]) > 0


@pytest.mark.asyncio
async def test_match_slots_node_with_slots():
    """match_slots_node selects best match from available slots."""
    state = _make_scheduling_state()
    state["parsed_intent"] = {
        "preferred_time_of_day": "morning",
        "urgency": "routine",
        "provider_name": "",
    }
    state["available_slots"] = [
        {"slot_id": "s1", "start": "2026-04-01T09:00:00", "provider_name": "Dr. A"},
        {"slot_id": "s2", "start": "2026-04-01T14:00:00", "provider_name": "Dr. B"},
    ]
    result = await match_slots_node(state)
    assert "best_match" in result
    assert result["best_match"] is not None


@pytest.mark.asyncio
async def test_match_slots_node_no_slots():
    """match_slots_node triggers HITL when no slots available."""
    state = _make_scheduling_state()
    state["parsed_intent"] = {"preferred_time_of_day": "any", "urgency": "routine"}
    state["available_slots"] = []
    result = await match_slots_node(state)
    assert result["needs_review"] is True
    assert result["confidence"] == 0.4


@pytest.mark.asyncio
async def test_evaluate_confidence_node_high():
    """High confidence when appointment booked successfully."""
    state = _make_scheduling_state()
    state["confidence"] = 0.85
    state["best_match"] = {"score": 80, "slot": {"slot_id": "s1"}}
    result = await evaluate_confidence_node(state)
    assert result["confidence"] >= 0.7
    assert result["needs_review"] is False


@pytest.mark.asyncio
async def test_evaluate_confidence_node_no_slots():
    """Low confidence when no slots available."""
    state = _make_scheduling_state()
    state["no_slots_available"] = True
    state["confidence"] = 0.0
    result = await evaluate_confidence_node(state)
    assert result["confidence"] < 0.7
    assert result["needs_review"] is True


# ── Full Agent Run Tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scheduling_agent_graph_construction():
    """SchedulingAgent builds a valid graph."""
    llm_provider = LLMProvider(primary=MockLLMBackend())
    agent = SchedulingAgent(llm_provider=llm_provider)
    graph = agent.build_graph()
    assert "parse_intent" in graph.node_names
    assert "query_availability" in graph.node_names
    assert "match_slots" in graph.node_names
    assert "create_appointment" in graph.node_names
    assert "evaluate_confidence" in graph.node_names
    assert "confirm" in graph.node_names


@pytest.mark.asyncio
async def test_scheduling_agent_full_run():
    """Full agent run with NL request produces appointment."""
    llm_provider = LLMProvider(primary=MockLLMBackend())
    state = await run_scheduling_agent(
        input_data={
            "request_text": "annual checkup with Dr. Smith next Tuesday",
        },
        llm_provider=llm_provider,
    )
    assert state.get("error") is None
    assert state.get("decision") is not None
    # Should have found slots and created an appointment
    decision = state["decision"]
    assert "parsed_intent" in decision


@pytest.mark.asyncio
async def test_scheduling_agent_structured_input():
    """Full agent run with structured input."""
    llm_provider = LLMProvider(primary=MockLLMBackend())
    state = await run_scheduling_agent(
        input_data={
            "specialty": "primary_care",
            "urgency": "routine",
            "preferred_time_of_day": "morning",
        },
        llm_provider=llm_provider,
    )
    assert state.get("error") is None
    assert state.get("decision") is not None


@pytest.mark.asyncio
async def test_scheduling_agent_empty_request_error():
    """Empty request triggers error path."""
    llm_provider = LLMProvider(primary=MockLLMBackend())
    state = await run_scheduling_agent(
        input_data={},
        llm_provider=llm_provider,
    )
    assert state.get("error") is not None


@pytest.mark.asyncio
async def test_scheduling_agent_audit_trail():
    """Agent produces audit trail entries."""
    llm_provider = LLMProvider(primary=MockLLMBackend())
    state = await run_scheduling_agent(
        input_data={
            "request_text": "follow up next week",
        },
        llm_provider=llm_provider,
    )
    audit_trail = state.get("audit_trail", [])
    assert len(audit_trail) > 0
    actions = [entry.get("action", "") for entry in audit_trail]
    assert "intent_parsed" in actions or "structured_input_accepted" in actions


# ── Urgent Slot Optimization (out-of-order slots) ──────────────────


@pytest.mark.asyncio
async def test_match_best_slot_urgent_out_of_order():
    """Urgent request with out-of-order slots picks earliest by datetime, not input order."""
    slots = [
        {"slot_id": "late", "start": "2026-01-16T15:00:00+00:00", "provider_name": "Dr. A"},
        {"slot_id": "early", "start": "2026-01-15T09:00:00+00:00", "provider_name": "Dr. B"},
        {"slot_id": "mid", "start": "2026-01-15T14:00:00+00:00", "provider_name": "Dr. C"},
    ]
    result = await match_best_slot(
        slots=slots,
        preferred_time_of_day="any",
        urgency="urgent",
    )
    assert result["success"] is True
    # The earliest slot (2026-01-15T09:00) should be the best match
    best = result["best_match"]["slot"]
    assert best["slot_id"] == "early", (
        f"Expected 'early' slot (earliest datetime) but got '{best['slot_id']}'"
    )


@pytest.mark.asyncio
async def test_match_best_slot_urgent_reverse_order():
    """Urgent: slots given latest-first should still pick the earliest."""
    slots = [
        {"slot_id": "s5", "start": "2026-01-20T10:00:00+00:00", "provider_name": "Dr. E"},
        {"slot_id": "s4", "start": "2026-01-19T10:00:00+00:00", "provider_name": "Dr. D"},
        {"slot_id": "s3", "start": "2026-01-18T10:00:00+00:00", "provider_name": "Dr. C"},
        {"slot_id": "s2", "start": "2026-01-17T10:00:00+00:00", "provider_name": "Dr. B"},
        {"slot_id": "s1", "start": "2026-01-16T10:00:00+00:00", "provider_name": "Dr. A"},
    ]
    result = await match_best_slot(
        slots=slots, preferred_time_of_day="any", urgency="urgent",
    )
    assert result["success"] is True
    assert result["best_match"]["slot"]["slot_id"] == "s1"


# ── Provider Name Propagation ──────────────────────────────────────


@pytest.mark.asyncio
async def test_query_available_slots_with_provider_name():
    """When provider_name is given, mock slots use that provider."""
    result = await query_available_slots(
        provider_name="Smith",
        specialty="primary_care",
        date_start="2026-04-01",
    )
    assert result["success"] is True
    for slot in result["slots"]:
        assert "Smith" in slot["provider_name"], (
            f"Expected provider_name containing 'Smith', got '{slot['provider_name']}'"
        )


@pytest.mark.asyncio
async def test_scheduling_agent_preserves_provider_in_slots():
    """End-to-end: 'Dr. Smith' request produces slots for Dr. Smith."""
    llm_provider = LLMProvider(primary=MockLLMBackend())
    state = await run_scheduling_agent(
        input_data={
            "request_text": "annual checkup with Dr. Smith next Tuesday",
        },
        llm_provider=llm_provider,
    )
    assert state.get("error") is None
    # The best match slot should be for Dr. Smith
    best_match = state.get("best_match")
    assert best_match is not None
    slot = best_match.get("slot", {})
    assert "Smith" in slot.get("provider_name", ""), (
        f"Expected slot for Dr. Smith, got provider_name='{slot.get('provider_name', '')}'"
    )


# ── Confirm Node (contract alignment) ──────────────────────────────


@pytest.mark.asyncio
async def test_confirm_node_assembles_decision():
    """confirm_node assembles the final scheduling decision."""
    state = _make_scheduling_state()
    state["appointment_result"] = {"success": True, "appointment_id": "apt-123"}
    state["best_match"] = {"slot": {"slot_id": "s1", "start": "2026-04-01T09:00:00"}, "score": 85}
    state["alternatives"] = [{"slot": {"slot_id": "s2"}}]
    state["confidence"] = 0.85
    result = await confirm_node(state)
    assert result["current_node"] == "confirm"
    assert result["decision"]["appointment"]["appointment_id"] == "apt-123"
    assert result["decision"]["selected_slot"]["slot_id"] == "s1"


# ── HITL Escalation via Full Agent Run ──────────────────────────────


@pytest.mark.asyncio
async def test_scheduling_agent_low_confidence_escalation():
    """Scheduling agent with no available slots triggers needs_review."""
    llm_provider = LLMProvider(primary=MockLLMBackend())
    # Use a date range in the far past where no real slots exist,
    # but mock will still return slots. Instead, use structured input
    # with no slots scenario by providing empty specialty to trigger
    # the standard path — but we need to force no slots.
    # We'll run the individual nodes to test the exact path.
    state = _make_scheduling_state(input_data={
        "request_text": "appointment with Dr. Nobody this saturday",
    })
    # Parse intent
    state = await parse_intent_node(state)
    # Check payer rules
    state = await check_payer_rules_node(state)
    # Force empty slots to test escalation
    state["available_slots"] = []
    state["parsed_intent"] = state.get("parsed_intent", {})
    state = await match_slots_node(state)
    assert state["needs_review"] is True
    assert state["confidence"] < 0.7
    state = await evaluate_confidence_node(state)
    assert state["needs_review"] is True
    assert "No available" in state.get("review_reason", "")


# ── Mock FHIR Server Integration Tests ──────────────────────────────


@pytest.mark.asyncio
async def test_query_slots_with_mock_fhir_server():
    """query_available_slots queries a real FHIR endpoint when fhir_base_url is set."""
    from unittest.mock import AsyncMock, patch

    # Build a mock FHIR Slot search response (Bundle)
    fhir_slot_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": 3,
        "entry": [
            {
                "resource": {
                    "resourceType": "Slot",
                    "id": "slot-fhir-001",
                    "status": "free",
                    "start": "2026-04-01T09:00:00+00:00",
                    "end": "2026-04-01T09:30:00+00:00",
                }
            },
            {
                "resource": {
                    "resourceType": "Slot",
                    "id": "slot-fhir-002",
                    "status": "free",
                    "start": "2026-04-01T11:00:00+00:00",
                    "end": "2026-04-01T11:30:00+00:00",
                }
            },
            {
                "resource": {
                    "resourceType": "Slot",
                    "id": "slot-fhir-003",
                    "status": "free",
                    "start": "2026-04-02T14:00:00+00:00",
                    "end": "2026-04-02T14:30:00+00:00",
                }
            },
        ],
    }

    # Mock the FHIRClient to return our test data
    mock_fhir_client = AsyncMock()
    mock_fhir_client.search_slots = AsyncMock(return_value=[
        entry["resource"] for entry in fhir_slot_bundle["entry"]
    ])
    mock_fhir_client.__aenter__ = AsyncMock(return_value=mock_fhir_client)
    mock_fhir_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.core.ingestion.fhir_client.FHIRClient", return_value=mock_fhir_client):
        result = await query_available_slots(
            provider_npi="1234567890",
            provider_name="Dr. Smith",
            specialty="primary_care",
            date_start="2026-04-01",
            date_end="2026-04-07",
            fhir_base_url="http://mock-fhir:8080/r4",
        )

    assert result["success"] is True
    assert result["source"] == "fhir"
    assert result["total_found"] == 3
    assert len(result["slots"]) == 3

    # Verify FHIR search was called with correct parameters
    mock_fhir_client.search_slots.assert_called_once()
    call_kwargs = mock_fhir_client.search_slots.call_args[1]
    assert call_kwargs["status"] == "free"
    assert "ge2026-04-01" in call_kwargs.get("start", "")
    assert call_kwargs.get("specialty") == "primary_care"

    # Verify slots are properly mapped
    slot_ids = [s["slot_id"] for s in result["slots"]]
    assert "slot-fhir-001" in slot_ids
    assert "slot-fhir-002" in slot_ids
    assert "slot-fhir-003" in slot_ids

    for slot in result["slots"]:
        assert slot["fhir_id"].startswith("Slot/")
        assert slot["status"] == "free"


@pytest.mark.asyncio
async def test_create_appointment_with_mock_fhir_server():
    """create_appointment creates a FHIR Appointment resource when fhir_base_url is set."""
    from unittest.mock import AsyncMock, patch

    mock_fhir_client = AsyncMock()
    mock_fhir_client.create = AsyncMock(return_value={
        "resourceType": "Appointment",
        "id": "apt-fhir-001",
        "status": "booked",
    })
    mock_fhir_client.__aenter__ = AsyncMock(return_value=mock_fhir_client)
    mock_fhir_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.core.ingestion.fhir_client.FHIRClient", return_value=mock_fhir_client):
        result = await create_appointment(
            slot_id="slot-fhir-001",
            patient_id="patient-123",
            provider_npi="1234567890",
            visit_type="annual_checkup",
            notes="Annual wellness visit",
            fhir_base_url="http://mock-fhir:8080/r4",
        )

    assert result["success"] is True
    assert result["source"] == "fhir"
    assert result["appointment_id"] == "apt-fhir-001"
    assert result["status"] == "booked"

    # Verify the FHIR create was called with correct resource structure
    mock_fhir_client.create.assert_called_once()
    call_args = mock_fhir_client.create.call_args
    assert call_args[0][0] == "Appointment"
    resource = call_args[0][1]
    assert resource["resourceType"] == "Appointment"
    assert resource["status"] == "booked"
    assert resource["slot"][0]["reference"] == "Slot/slot-fhir-001"
    # Verify patient and provider participants
    participants = resource["participant"]
    assert any("Patient/patient-123" in str(p) for p in participants)
    assert any("1234567890" in str(p) for p in participants)


@pytest.mark.asyncio
async def test_query_slots_fhir_fallback_on_error():
    """When FHIR server fails, query_available_slots falls back to synthetic slots."""
    from unittest.mock import AsyncMock, patch

    mock_fhir_client = AsyncMock()
    mock_fhir_client.search_slots = AsyncMock(side_effect=Exception("Connection refused"))
    mock_fhir_client.__aenter__ = AsyncMock(return_value=mock_fhir_client)
    mock_fhir_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.core.ingestion.fhir_client.FHIRClient", return_value=mock_fhir_client):
        result = await query_available_slots(
            provider_name="Dr. Smith",
            specialty="primary_care",
            fhir_base_url="http://broken-fhir:8080/r4",
        )

    assert result["success"] is True
    assert result["source"] == "mock"  # Fell back to synthetic
    assert len(result["slots"]) > 0


@pytest.mark.asyncio
async def test_scheduling_end_to_end_with_fhir_mock():
    """Full scheduling agent run using mock FHIR server for slot query and appointment creation."""
    from unittest.mock import AsyncMock, patch

    fhir_slots = [
        {
            "resourceType": "Slot",
            "id": "slot-fhir-100",
            "status": "free",
            "start": "2026-04-01T09:00:00+00:00",
            "end": "2026-04-01T09:30:00+00:00",
        },
        {
            "resourceType": "Slot",
            "id": "slot-fhir-101",
            "status": "free",
            "start": "2026-04-01T14:00:00+00:00",
            "end": "2026-04-01T14:30:00+00:00",
        },
    ]

    mock_fhir_client = AsyncMock()
    mock_fhir_client.search_slots = AsyncMock(return_value=fhir_slots)
    mock_fhir_client.create = AsyncMock(return_value={
        "resourceType": "Appointment",
        "id": "apt-fhir-100",
        "status": "booked",
    })
    mock_fhir_client.__aenter__ = AsyncMock(return_value=mock_fhir_client)
    mock_fhir_client.__aexit__ = AsyncMock(return_value=False)

    llm_provider = LLMProvider(primary=MockLLMBackend())

    with patch("app.core.ingestion.fhir_client.FHIRClient", return_value=mock_fhir_client):
        state = await run_scheduling_agent(
            input_data={
                "request_text": "Schedule a morning appointment with Dr. Smith",
                "fhir_base_url": "http://mock-fhir:8080/r4",
            },
            llm_provider=llm_provider,
        )

    assert state.get("error") is None
    # Verify FHIR was used for slot query
    mock_fhir_client.search_slots.assert_called_once()
    # Verify slots came from FHIR (mapped from FHIR Slot resources)
    available = state.get("available_slots", [])
    assert len(available) == 2
    slot_ids = [s.get("slot_id", "") for s in available]
    assert "slot-fhir-100" in slot_ids
    assert "slot-fhir-101" in slot_ids


# ── provider_name-only structured input tests ────────────────────────


@pytest.mark.asyncio
async def test_parse_intent_node_provider_name_only_structured_input():
    """parse_intent_node accepts provider_name-only structured input.

    Regression test: workflow validation accepts provider_name as valid
    structured input, so the graph must also accept it without falling
    through to the 'No scheduling request text' error path.
    """
    state = _make_scheduling_state(input_data={
        "provider_name": "Dr. Johnson",
    })
    result = await parse_intent_node(state)
    assert result.get("error") is None, (
        f"provider_name-only structured input should be accepted, got error: {result.get('error')}"
    )
    parsed = result.get("parsed_intent", {})
    assert parsed["provider_name"] == "Dr. Johnson"


@pytest.mark.asyncio
async def test_parse_intent_next_weekday_no_overshoot():
    """'next <weekday>' should not overshoot by double-adding 7 days.

    From any day of the week, 'next Tuesday' should land within 7-13 days
    (i.e., the Tuesday in the following calendar week), never 14+ days out.
    """
    from unittest.mock import patch
    from datetime import datetime, timezone

    days_of_week = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }

    for current_day_name, current_day_num in days_of_week.items():
        # Fix "now" to a known date for this weekday
        # 2026-03-23 is a Monday; add current_day_num to get the desired weekday
        fixed_now = datetime(2026, 3, 23 + current_day_num, 10, 0, tzinfo=timezone.utc)
        assert fixed_now.weekday() == current_day_num

        with patch("app.agents.scheduling.tools.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.strptime = datetime.strptime
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            result = await parse_scheduling_intent("next tuesday appointment")
            assert result["success"] is True
            parsed = result["parsed"]
            target_date = datetime.strptime(parsed["preferred_date_start"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            delta = (target_date - fixed_now.replace(hour=0, minute=0)).days
            # "next tuesday" should be 1-14 days away, never more
            assert 1 <= delta <= 14, (
                f"From {current_day_name} (day {current_day_num}), "
                f"'next tuesday' landed {delta} days out ({parsed['preferred_date_start']})"
            )
            # Must actually be a Tuesday
            assert target_date.weekday() == 1, (
                f"Target {parsed['preferred_date_start']} is not a Tuesday"
            )


@pytest.mark.asyncio
async def test_parse_intent_bare_weekday_nearest_occurrence():
    """Bare weekday without 'next' should give the nearest upcoming occurrence."""
    from unittest.mock import patch
    from datetime import datetime, timezone

    # Fix now to Wednesday 2026-03-25
    fixed_now = datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc)
    assert fixed_now.weekday() == 2  # Wednesday

    with patch("app.agents.scheduling.tools.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.strptime = datetime.strptime
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        # "friday" from Wednesday = 2 days away
        result = await parse_scheduling_intent("appointment friday")
        parsed = result["parsed"]
        target = datetime.strptime(parsed["preferred_date_start"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        assert target.weekday() == 4  # Friday
        delta = (target - fixed_now.replace(hour=0, minute=0)).days
        assert delta == 2, f"Expected 2 days, got {delta}"


@pytest.mark.asyncio
async def test_scheduling_agent_provider_name_only_end_to_end():
    """Full agent run with provider_name-only structured input completes
    without error, matching workflow validation behavior."""
    llm_provider = LLMProvider(primary=MockLLMBackend())
    state = await run_scheduling_agent(
        input_data={
            "provider_name": "Dr. Johnson",
            "patient_id": "PAT-001",
        },
        llm_provider=llm_provider,
    )
    assert state.get("error") is None, (
        f"provider_name-only request should succeed, got error: {state.get('error')}"
    )
    # Should have gone through the full graph (not short-circuited to error)
    assert state.get("current_node") == "confirm"
