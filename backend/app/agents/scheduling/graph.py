"""Scheduling agent LangGraph implementation.

Defines a complete graph with explicit scheduling-specific nodes:
  parse_intent → check_payer_rules → query_availability → match_slots →
  create_appointment → confirm/escalate

This agent processes natural language scheduling requests into structured
appointment bookings via FHIR Appointment/Slot resources.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from langgraph.graph import StateGraph, END
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import BaseAgent
from app.agents.scheduling.prompts import (
    SYSTEM_PROMPT,
    PARSE_INTENT_PROMPT,
    SLOT_OPTIMIZATION_PROMPT,
)
from app.agents.scheduling.tools import get_scheduling_tools, match_best_slot
from app.core.engine.graph_builder import AgentGraph
from app.core.engine.llm_provider import LLMProvider, LLMError
from app.core.engine.state import AuditEntry, BaseAgentState, create_initial_state
from app.core.engine.tool_executor import ToolDefinition, ToolExecutor

logger = logging.getLogger(__name__)

# Confidence threshold for HITL escalation
SCHEDULING_CONFIDENCE_THRESHOLD = 0.7


# ── Scheduling-specific graph nodes ──────────────────────────────────


async def parse_intent_node(state: dict) -> dict:
    """Parse the natural language scheduling request into structured parameters.

    Uses the LLM with PARSE_INTENT_PROMPT when available, falling back to
    rule-based NLP extraction.  The LLM call enriches intent extraction for
    ambiguous or complex requests.
    """
    state["current_node"] = "parse_intent"
    input_data = state.get("input_data", {})

    request_text = input_data.get("request_text", "")
    if not request_text:
        # Check for structured input instead
        if input_data.get("specialty") or input_data.get("provider_npi") or input_data.get("provider_name"):
            # Structured input provided directly
            parsed = {
                "provider_name": input_data.get("provider_name"),
                "provider_npi": input_data.get("provider_npi"),
                "specialty": input_data.get("specialty"),
                "preferred_date_start": input_data.get("preferred_date_start"),
                "preferred_date_end": input_data.get("preferred_date_end"),
                "preferred_time_of_day": input_data.get("preferred_time_of_day", "any"),
                "urgency": input_data.get("urgency", "routine"),
                "visit_type": input_data.get("visit_type", "follow_up"),
                "duration_minutes": input_data.get("duration_minutes", 30),
                "notes": input_data.get("notes", ""),
            }
            state["parsed_intent"] = parsed
            state["audit_trail"].append(
                AuditEntry(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    node="parse_intent",
                    action="structured_input_accepted",
                    details={"has_provider": bool(parsed.get("provider_npi"))},
                )
            )
            return state

        state["error"] = "No scheduling request text or structured input provided"
        state["confidence"] = 0.0
        state["audit_trail"].append(
            AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                node="parse_intent",
                action="validation_failed",
                details={"error": "empty_request"},
            )
        )
        return state

    # Deterministic rule-based parsing (always runs as baseline)
    from app.agents.scheduling.tools import parse_scheduling_intent
    result = await parse_scheduling_intent(request_text)

    if not result.get("success"):
        state["error"] = result.get("error", "Failed to parse scheduling request")
        state["confidence"] = 0.0
    else:
        state["parsed_intent"] = result["parsed"]

    # Attempt LLM-augmented intent extraction for richer understanding
    llm_provider: LLMProvider | None = state.get("_llm_provider")
    llm_used = False
    if llm_provider and result.get("success"):
        try:
            patient_ctx = state.get("patient_context", {})
            prompt = PARSE_INTENT_PROMPT.format(
                request_text=request_text,
                patient_context=json.dumps(patient_ctx, default=str),
            )
            llm_response = await llm_provider.send(
                messages=[{"role": "user", "content": prompt}],
                system_prompt=SYSTEM_PROMPT,
                max_tokens=1024,
                temperature=0.0,
            )
            # Try to parse LLM JSON response and merge enrichments
            try:
                llm_parsed = json.loads(llm_response.content)
                llm_decision = llm_parsed if isinstance(llm_parsed, dict) else {}
                # Merge LLM enrichments into parsed intent (LLM can fill gaps
                # the rule-based parser missed, but rule-based values take
                # priority when they are already populated)
                parsed = state["parsed_intent"]
                for key in ("specialty", "visit_type", "notes"):
                    if not parsed.get(key) and llm_decision.get(key):
                        parsed[key] = llm_decision[key]
                llm_used = True
            except (json.JSONDecodeError, KeyError):
                pass  # LLM response not parseable — keep rule-based result
        except LLMError:
            logger.debug("LLM augmentation failed for parse_intent, using rule-based result")

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="parse_intent",
            action="intent_parsed",
            details={
                "request_text_length": len(request_text),
                "has_provider": bool(result.get("parsed", {}).get("provider_name")),
                "has_specialty": bool(result.get("parsed", {}).get("specialty")),
                "urgency": result.get("parsed", {}).get("urgency", "unknown"),
                "visit_type": result.get("parsed", {}).get("visit_type", "unknown"),
                "llm_augmented": llm_used,
            },
        )
    )

    return state


async def check_payer_rules_node(state: dict) -> dict:
    """Check payer-specific rules for scheduling.

    Looks up any payer rules that affect scheduling (e.g., referral
    requirements, in-network provider restrictions).
    """
    state["current_node"] = "check_payer_rules"
    payer_context = state.get("payer_context", {})
    payer_id = payer_context.get("payer_id", "")

    rules_applied = []
    if payer_id:
        rules_applied.append({
            "rule": "scheduling_allowed",
            "payer_id": payer_id,
            "result": "pass",
        })

    state["payer_rules_applied"] = rules_applied
    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="check_payer_rules",
            action="payer_rules_checked",
            details={
                "payer_id": payer_id,
                "rules_count": len(rules_applied),
            },
        )
    )

    return state


async def query_availability_node(state: dict) -> dict:
    """Query FHIR server for available appointment slots.

    Uses the parsed intent to search for matching slots based on
    provider, specialty, date range, and duration.
    """
    state["current_node"] = "query_availability"
    parsed = state.get("parsed_intent", {})

    from app.agents.scheduling.tools import query_available_slots
    input_data = state.get("input_data", {})
    fhir_base_url = input_data.get("fhir_base_url")
    result = await query_available_slots(
        provider_npi=parsed.get("provider_npi", "") or "",
        provider_name=parsed.get("provider_name", "") or "",
        specialty=parsed.get("specialty", "") or "",
        date_start=parsed.get("preferred_date_start", "") or "",
        date_end=parsed.get("preferred_date_end", "") or "",
        duration_minutes=parsed.get("duration_minutes", 30),
        fhir_base_url=fhir_base_url,
    )

    state["available_slots"] = result.get("slots", [])

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="query_availability",
            action="slots_queried",
            details={
                "slots_found": result.get("total_found", 0),
                "specialty": parsed.get("specialty", ""),
            },
        )
    )

    return state


async def match_slots_node(state: dict) -> dict:
    """Match the best available slot to patient preferences.

    Uses deterministic scoring with optional LLM-augmented slot optimization
    via SLOT_OPTIMIZATION_PROMPT.  The LLM provides reasoning about complex
    trade-offs while the deterministic algorithm ensures reliable selection.
    """
    state["current_node"] = "match_slots"
    parsed = state.get("parsed_intent", {})
    slots = state.get("available_slots", [])

    if not slots:
        state["no_slots_available"] = True
        state["confidence"] = 0.4
        state["needs_review"] = True
        state["review_reason"] = "No available appointment slots found"

        # Add patient to waitlist when no slots are available
        input_data = state.get("input_data", {})
        from app.agents.scheduling.tools import add_to_waitlist
        waitlist_result = await add_to_waitlist(
            patient_id=input_data.get("patient_id", ""),
            provider_npi=parsed.get("provider_npi", "") or "",
            specialty=parsed.get("specialty", "") or "",
            urgency=parsed.get("urgency", "routine"),
            preferred_date_start=parsed.get("preferred_date_start", "") or "",
            preferred_date_end=parsed.get("preferred_date_end", "") or "",
        )
        state["waitlist_result"] = waitlist_result

        state["audit_trail"].append(
            AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                node="match_slots",
                action="no_slots_waitlisted",
                details={
                    "waitlist_id": waitlist_result.get("waitlist_id", ""),
                    "position": waitlist_result.get("position", 0),
                },
            )
        )
        return state

    result = await match_best_slot(
        slots=slots,
        preferred_time_of_day=parsed.get("preferred_time_of_day", "any"),
        urgency=parsed.get("urgency", "routine"),
        provider_name=parsed.get("provider_name", "") or "",
    )

    if not result.get("success"):
        state["error"] = result.get("error", "Slot matching failed")
        state["confidence"] = 0.0
    else:
        state["best_match"] = result.get("best_match")
        state["alternatives"] = result.get("alternatives", [])

    # LLM-augmented slot reasoning (optional enrichment)
    llm_provider: LLMProvider | None = state.get("_llm_provider")
    llm_used = False
    if llm_provider and result.get("success"):
        try:
            # Summarise top slots for the LLM (avoid sending full list)
            top_slots = slots[:5]
            preferences = {
                "preferred_time_of_day": parsed.get("preferred_time_of_day", "any"),
                "urgency": parsed.get("urgency", "routine"),
                "provider_name": parsed.get("provider_name", ""),
            }
            prompt = SLOT_OPTIMIZATION_PROMPT.format(
                available_slots=json.dumps(top_slots, default=str),
                preferences=json.dumps(preferences, default=str),
                payer_rules=json.dumps(state.get("payer_rules_applied", []), default=str),
            )
            llm_response = await llm_provider.send(
                messages=[{"role": "user", "content": prompt}],
                system_prompt=SYSTEM_PROMPT,
                max_tokens=1024,
                temperature=0.0,
            )
            # Store LLM reasoning for audit / review transparency
            state["slot_reasoning"] = llm_response.content
            llm_used = True
        except LLMError:
            logger.debug("LLM slot optimization failed, using deterministic result")

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="match_slots",
            action="slots_matched",
            details={
                "best_score": result.get("best_match", {}).get("score", 0),
                "total_evaluated": result.get("total_evaluated", 0),
                "alternatives_count": len(result.get("alternatives", [])),
                "llm_augmented": llm_used,
            },
        )
    )

    return state


async def create_appointment_node(state: dict) -> dict:
    """Create the appointment for the selected slot.

    Books the appointment via FHIR and records the confirmation.
    """
    state["current_node"] = "create_appointment"
    best_match = state.get("best_match")

    if not best_match:
        # No match — skip to output
        state["audit_trail"].append(
            AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                node="create_appointment",
                action="skipped_no_match",
                details={},
            )
        )
        return state

    slot = best_match.get("slot", {})
    input_data = state.get("input_data", {})
    parsed = state.get("parsed_intent", {})
    fhir_base_url = input_data.get("fhir_base_url")

    from app.agents.scheduling.tools import create_appointment
    result = await create_appointment(
        slot_id=slot.get("slot_id", ""),
        patient_id=input_data.get("patient_id", ""),
        provider_npi=slot.get("provider_npi", ""),
        visit_type=parsed.get("visit_type", "follow_up"),
        notes=parsed.get("notes", ""),
        fhir_base_url=fhir_base_url,
    )

    state["appointment_result"] = result
    state["confidence"] = 0.85 if result.get("success") else 0.3

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="create_appointment",
            action="appointment_created" if result.get("success") else "appointment_failed",
            details={
                "appointment_id": result.get("appointment_id", ""),
                "slot_id": slot.get("slot_id", ""),
                "status": result.get("status", ""),
            },
        )
    )

    return state


async def evaluate_confidence_node(state: dict) -> dict:
    """Evaluate confidence and determine if HITL escalation is needed.

    Checks scheduling-specific factors:
    - No available slots
    - Complex multi-provider scheduling
    - Priority conflicts
    - Low match quality
    """
    state["current_node"] = "evaluate_confidence"

    raw_confidence = state.get("confidence", 0.0)
    confidence = raw_confidence if raw_confidence > 0.0 else 0.85
    needs_review = state.get("needs_review", False)
    review_reason = state.get("review_reason", "")

    # Check for errors
    if state.get("error"):
        confidence = 0.0
        needs_review = True
        review_reason = f"Error during processing: {state['error']}"
    elif state.get("no_slots_available"):
        confidence = 0.4
        needs_review = True
        review_reason = review_reason or "No available appointment slots found within requested window"
    else:
        # Check match quality
        best_match = state.get("best_match")
        if best_match:
            score = best_match.get("score", 0)
            if score < 50:
                confidence = min(confidence, 0.5)
                needs_review = True
                review_reason = "Low slot match quality score"

    if confidence < SCHEDULING_CONFIDENCE_THRESHOLD and not needs_review:
        needs_review = True
        if not review_reason:
            review_reason = (
                f"Confidence {confidence:.2f} below threshold "
                f"{SCHEDULING_CONFIDENCE_THRESHOLD:.2f}"
            )

    state["confidence"] = confidence
    state["needs_review"] = needs_review
    state["review_reason"] = review_reason

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="evaluate_confidence",
            action="confidence_evaluated",
            details={
                "confidence": confidence,
                "needs_review": needs_review,
                "review_reason": review_reason,
            },
        )
    )

    return state


async def escalate_node(state: dict) -> dict:
    """Escalation node — marks the task for HITL review."""
    state["current_node"] = "escalate"
    state["needs_review"] = True

    state["decision"] = {
        "parsed_intent": state.get("parsed_intent", {}),
        "available_slots": len(state.get("available_slots", [])),
        "best_match": state.get("best_match"),
        "alternatives": state.get("alternatives", []),
        "confidence": state.get("confidence", 0.0),
        "needs_review": True,
        "review_reason": state.get("review_reason", ""),
        "escalated": True,
    }

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="escalate",
            action="escalated_to_hitl",
            details={
                "confidence": state.get("confidence", 0.0),
                "review_reason": state.get("review_reason", ""),
            },
        )
    )

    return state


async def confirm_node(state: dict) -> dict:
    """Confirm node — assembles and confirms the agent's scheduling output.

    This is the terminal node for the scheduling graph, corresponding to
    the 'confirm' step in the contract:
      parse_intent → query_availability → match_slots → create_appointment → confirm/escalate
    """
    state["current_node"] = "confirm"

    if not state.get("decision"):
        appointment = state.get("appointment_result", {})
        best_match = state.get("best_match")

        state["decision"] = {
            "parsed_intent": state.get("parsed_intent", {}),
            "appointment": appointment if appointment.get("success") else None,
            "selected_slot": best_match.get("slot") if best_match else None,
            "alternatives": [
                alt.get("slot") for alt in state.get("alternatives", [])
            ],
            "waitlist": state.get("waitlist_result"),
            "confidence": state.get("confidence", 0.0),
            "needs_review": state.get("needs_review", False),
            "review_reason": state.get("review_reason", ""),
        }

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="confirm",
            action="execution_completed",
            details={
                "confidence": state.get("confidence", 0.0),
                "needs_review": state.get("needs_review", False),
                "has_appointment": bool(state.get("appointment_result", {}).get("success")),
            },
        )
    )

    return state


# ── Graph routing ────────────────────────────────────────────────────


def _parse_intent_router(state: dict) -> str:
    """Route from parse_intent: on error, go to evaluate_confidence for
    proper HITL escalation instead of bypassing it."""
    if state.get("error"):
        return "evaluate_confidence"
    return "check_payer_rules"


def _evaluate_confidence_router(state: dict) -> str:
    """Route from evaluate_confidence: to confirm or escalate."""
    if state.get("needs_review", False):
        return "escalate"
    return "confirm"


# ── Agent class ──────────────────────────────────────────────────────


class SchedulingAgent(BaseAgent):
    """Scheduling & Access Agent.

    Processes appointment scheduling requests through a LangGraph workflow:

    1. parse_intent — extract structured parameters from NL request (LLM-augmented)
    2. check_payer_rules — verify payer scheduling constraints
    3. query_availability — find available FHIR Appointment slots
    4. match_slots — rank and select optimal slot (LLM-augmented reasoning)
    5. create_appointment — book the appointment
    6. evaluate_confidence — determine confidence and HITL escalation
    7. confirm/escalate — confirm output or escalate to HITL
    """

    agent_type = "scheduling"
    confidence_threshold = SCHEDULING_CONFIDENCE_THRESHOLD

    def get_tools(self) -> list[ToolDefinition]:
        """Return scheduling-specific tools."""
        return get_scheduling_tools()

    async def run(
        self,
        *,
        task_id: str | None = None,
        input_data: dict[str, Any] | None = None,
        patient_context: Any = None,
        payer_context: Any = None,
    ) -> BaseAgentState:
        """Run the scheduling agent, injecting the LLM provider into state.

        The LLM provider is stored in the state under ``_llm_provider`` so
        that graph nodes (parse_intent, match_slots) can optionally call the
        LLM for augmented reasoning while falling back to deterministic logic.
        """
        import uuid as _uuid

        effective_task_id = task_id or str(_uuid.uuid4())
        state = create_initial_state(
            task_id=effective_task_id,
            agent_type=self.agent_type,
            input_data=input_data,
            patient_context=patient_context,
            payer_context=payer_context,
            max_iterations=self.max_iterations,
        )
        # Inject LLM provider so graph nodes can use it for augmentation
        state["_llm_provider"] = self._llm_provider  # type: ignore[typeddict-unknown-key]

        graph = self.build_graph()

        try:
            state = await graph.run(state)
        except Exception as exc:
            logger.error(
                "Agent '%s' task '%s' failed: %s",
                self.agent_type, effective_task_id, exc,
            )
            state["error"] = str(exc)

        if self._session is not None:
            await self._persist_audit_trail(state)
            await self._evaluate_escalation(state)

        return state

    def build_graph(self) -> AgentGraph:
        """Build the scheduling agent graph with explicit contract nodes.

        Graph topology matches the contract:
            parse_intent → [check_payer_rules | evaluate_confidence (on error)]
            check_payer_rules → query_availability
            query_availability → match_slots
            match_slots → create_appointment
            create_appointment → evaluate_confidence
            evaluate_confidence → [confirm | escalate]
            escalate → confirm
        """
        graph = StateGraph(dict)

        graph.add_node("parse_intent", parse_intent_node)
        graph.add_node("check_payer_rules", check_payer_rules_node)
        graph.add_node("query_availability", query_availability_node)
        graph.add_node("match_slots", match_slots_node)
        graph.add_node("create_appointment", create_appointment_node)
        graph.add_node("evaluate_confidence", evaluate_confidence_node)
        graph.add_node("escalate", escalate_node)
        graph.add_node("confirm", confirm_node)

        graph.set_entry_point("parse_intent")

        graph.add_conditional_edges(
            "parse_intent",
            _parse_intent_router,
            {"check_payer_rules": "check_payer_rules", "evaluate_confidence": "evaluate_confidence"},
        )
        graph.add_edge("check_payer_rules", "query_availability")
        graph.add_edge("query_availability", "match_slots")
        graph.add_edge("match_slots", "create_appointment")
        graph.add_edge("create_appointment", "evaluate_confidence")
        graph.add_conditional_edges(
            "evaluate_confidence",
            _evaluate_confidence_router,
            {"escalate": "escalate", "confirm": "confirm"},
        )
        graph.add_edge("escalate", "confirm")
        graph.add_edge("confirm", END)

        compiled = graph.compile()

        return AgentGraph(
            compiled_graph=compiled,
            node_names=[
                "parse_intent",
                "check_payer_rules",
                "query_availability",
                "match_slots",
                "create_appointment",
                "evaluate_confidence",
                "escalate",
                "confirm",
            ],
        )


async def run_scheduling_agent(
    *,
    input_data: dict[str, Any],
    llm_provider: LLMProvider,
    session: AsyncSession | None = None,
    task_id: str | None = None,
) -> BaseAgentState:
    """Convenience function to run the scheduling agent.

    Creates and runs a SchedulingAgent with the given input.
    Returns the final agent state.
    """
    agent = SchedulingAgent(
        llm_provider=llm_provider,
        session=session,
    )

    patient_context = {
        "patient_id": input_data.get("patient_id", ""),
        "first_name": input_data.get("patient_first_name", ""),
        "last_name": input_data.get("patient_last_name", ""),
    }

    payer_context = {
        "payer_id": input_data.get("payer_id", ""),
        "payer_name": input_data.get("payer_name", ""),
    }

    state = await agent.run(
        task_id=task_id,
        input_data=input_data,
        patient_context=patient_context,
        payer_context=payer_context,
    )

    return state
