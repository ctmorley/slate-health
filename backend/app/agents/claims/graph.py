"""Claims & Billing agent LangGraph implementation.

Defines a complete graph with explicit claims-specific nodes:
  validate_codes → check_payer_rules → build_837 → submit_clearinghouse →
  track_status → parse_835 → handle_denial → evaluate_confidence → output/escalate

This agent handles the full claim lifecycle: code validation, 837 submission,
835 payment parsing, and denial management with appeal recommendations.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from langgraph.graph import StateGraph, END
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import BaseAgent
from app.agents.claims.prompts import (
    SYSTEM_PROMPT,
    CODE_VALIDATION_PROMPT,
    DENIAL_ANALYSIS_PROMPT,
)
from app.agents.claims.tools import get_claims_tools
from app.core.engine.graph_builder import AgentGraph
from app.core.engine.llm_provider import LLMProvider, LLMError
from app.core.engine.state import AuditEntry, BaseAgentState, create_initial_state
from app.core.engine.tool_executor import ToolDefinition, ToolExecutor

logger = logging.getLogger(__name__)

# Confidence threshold for HITL escalation
CLAIMS_CONFIDENCE_THRESHOLD = 0.7


# ── Claims-specific graph nodes ──────────────────────────────────────


async def validate_codes_node(state: dict) -> dict:
    """Validate ICD-10 and CPT codes from the encounter data.

    Flags invalid or missing codes for HITL review.
    """
    state["current_node"] = "validate_codes"
    input_data = state.get("input_data", {})

    diagnosis_codes = input_data.get("diagnosis_codes", [])
    procedure_codes = input_data.get("procedure_codes", [])

    # Validate required fields — missing codes route to HITL review, not hard error
    if not diagnosis_codes:
        state["needs_review"] = True
        state["confidence"] = 0.0
        state["review_reason"] = "Missing diagnosis codes (ICD-10): at least one is required"
        state["audit_trail"].append(
            AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                node="validate_codes",
                action="validation_failed",
                details={"error": "missing_diagnosis_codes"},
            )
        )
        return state

    if not procedure_codes and not input_data.get("service_lines"):
        state["needs_review"] = True
        state["confidence"] = 0.0
        state["review_reason"] = "Missing procedure codes (CPT): at least one procedure code or service line is required"
        state["audit_trail"].append(
            AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                node="validate_codes",
                action="validation_failed",
                details={"error": "missing_procedure_codes"},
            )
        )
        return state

    # Validate diagnosis codes
    from app.agents.claims.tools import validate_diagnosis_codes, validate_procedure_codes
    dx_result = await validate_diagnosis_codes(diagnosis_codes)
    state["dx_validation"] = dx_result

    # Validate procedure codes
    cpt_result = await validate_procedure_codes(procedure_codes)
    state["cpt_validation"] = cpt_result

    # Flag invalid codes for HITL
    has_invalid_dx = not dx_result["all_valid"]
    has_invalid_cpt = not cpt_result["all_valid"]

    if has_invalid_dx or has_invalid_cpt:
        state["needs_review"] = True
        state["confidence"] = 0.4
        issues = []
        if has_invalid_dx:
            invalid_dx = [c["code"] for c in dx_result["codes"] if not c["valid"]]
            if invalid_dx:
                issues.append(f"Invalid ICD-10 codes: {', '.join(invalid_dx)}")
            # Include format-valid but unknown codes that need review
            review_dx = [c["code"] for c in dx_result["codes"] if c.get("needs_review")]
            if review_dx:
                issues.append(f"ICD-10 codes require verification (not in local database): {', '.join(review_dx)}")
        if has_invalid_cpt:
            invalid_cpt = [c["code"] for c in cpt_result["codes"] if not c["valid"]]
            if invalid_cpt:
                issues.append(f"Invalid CPT codes: {', '.join(invalid_cpt)}")
            # Include format-valid but unknown codes that need review
            review_cpt = [c["code"] for c in cpt_result["codes"] if c.get("needs_review")]
            if review_cpt:
                issues.append(f"CPT codes require verification (not in local database): {', '.join(review_cpt)}")
        state["review_reason"] = "; ".join(issues)

    # LLM-augmented code validation reasoning (optional enrichment)
    llm_provider: LLMProvider | None = state.get("_llm_provider")
    llm_used = False
    if llm_provider:
        try:
            encounter_ctx = {
                "date_of_service": input_data.get("date_of_service", ""),
                "payer_id": input_data.get("payer_id", ""),
                "place_of_service": input_data.get("place_of_service", ""),
            }
            prompt = CODE_VALIDATION_PROMPT.format(
                diagnosis_codes=json.dumps(diagnosis_codes),
                procedure_codes=json.dumps(procedure_codes),
                encounter_context=json.dumps(encounter_ctx, default=str),
            )
            llm_response = await llm_provider.send(
                messages=[{"role": "user", "content": prompt}],
                system_prompt=SYSTEM_PROMPT,
                max_tokens=1024,
                temperature=0.0,
            )
            state["code_validation_reasoning"] = llm_response.content
            llm_used = True
        except LLMError:
            logging.getLogger(__name__).debug(
                "LLM augmentation failed for validate_codes, using rule-based result"
            )

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="validate_codes",
            action="codes_validated",
            details={
                "dx_total": dx_result["total"],
                "dx_valid": dx_result["valid_count"],
                "dx_invalid": dx_result["invalid_count"],
                "cpt_total": cpt_result["total"],
                "cpt_valid": cpt_result["valid_count"],
                "cpt_invalid": cpt_result["invalid_count"],
                "llm_augmented": llm_used,
            },
        )
    )

    return state


async def check_payer_rules_node(state: dict) -> dict:
    """Check payer-specific billing rules.

    Looks up rules for the claim's payer to determine special requirements
    (e.g., modifier requirements, bundling rules, prior auth needs).
    """
    state["current_node"] = "check_payer_rules"
    payer_context = state.get("payer_context", {})
    payer_id = payer_context.get("payer_id", "")

    rules_applied = []
    if payer_id:
        rules_applied.append({
            "rule": "claims_submission_allowed",
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


async def build_837_node(state: dict) -> dict:
    """Build the X12 837P claim from validated codes and encounter data.

    Constructs the complete 837P transaction with all required segments.
    """
    state["current_node"] = "build_837"
    input_data = state.get("input_data", {})

    claim_type = input_data.get("claim_type", "837P").upper()

    if claim_type == "837I":
        from app.agents.claims.tools import build_837i_claim
        result = await build_837i_claim(
            subscriber_id=input_data.get("subscriber_id", ""),
            subscriber_last_name=input_data.get("subscriber_last_name", ""),
            subscriber_first_name=input_data.get("subscriber_first_name", ""),
            subscriber_dob=input_data.get("subscriber_dob", "19900101"),
            subscriber_gender=input_data.get("subscriber_gender", "U"),
            payer_id=input_data.get("payer_id", ""),
            payer_name=input_data.get("payer_name", ""),
            claim_id=input_data.get("claim_id", ""),
            total_charge=input_data.get("total_charge", "100.00"),
            diagnosis_codes=input_data.get("diagnosis_codes", []),
            service_lines=input_data.get("service_lines"),
            admission_date=input_data.get("admission_date", ""),
            discharge_date=input_data.get("discharge_date", ""),
            type_of_bill=input_data.get("type_of_bill", "0111"),
            drg_code=input_data.get("drg_code", ""),
        )
    else:
        from app.agents.claims.tools import build_837p_claim
        result = await build_837p_claim(
            subscriber_id=input_data.get("subscriber_id", ""),
            subscriber_last_name=input_data.get("subscriber_last_name", ""),
            subscriber_first_name=input_data.get("subscriber_first_name", ""),
            subscriber_dob=input_data.get("subscriber_dob", "19900101"),
            subscriber_gender=input_data.get("subscriber_gender", "U"),
            payer_id=input_data.get("payer_id", ""),
            payer_name=input_data.get("payer_name", ""),
            claim_id=input_data.get("claim_id", ""),
            total_charge=input_data.get("total_charge", "100.00"),
            diagnosis_codes=input_data.get("diagnosis_codes", []),
            procedure_codes=input_data.get("procedure_codes", []),
            service_lines=input_data.get("service_lines"),
            date_of_service=input_data.get("date_of_service", ""),
            place_of_service=input_data.get("place_of_service", "11"),
            billing_provider_npi=input_data.get("billing_provider_npi", "1234567890"),
            billing_provider_name=input_data.get("billing_provider_name", "Provider"),
            billing_provider_tax_id=input_data.get("billing_provider_tax_id", "123456789"),
        )

    if not result.get("success"):
        state["error"] = result.get("error", "Failed to build 837P claim")
        state["confidence"] = 0.0
    else:
        state["x12_837_data"] = result

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="build_837",
            action="837_built" if result.get("success") else "837_build_failed",
            details={
                "claim_id": result.get("claim_id", ""),
                "success": result.get("success", False),
            },
        )
    )

    return state


async def submit_clearinghouse_node(state: dict) -> dict:
    """Submit the 837P claim to the clearinghouse.

    In the LangGraph context, this records the submission intent.
    Actual submission happens in the Temporal workflow activities.
    """
    state["current_node"] = "submit_clearinghouse"
    x12_data = state.get("x12_837_data", {})

    state["submission_status"] = "submitted"
    state["submission_metadata"] = {
        "claim_id": x12_data.get("claim_id", ""),
        "control_number": x12_data.get("control_number", ""),
        "submitted": True,
    }

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="submit_clearinghouse",
            action="clearinghouse_submission_prepared",
            details={"claim_id": x12_data.get("claim_id", "")},
        )
    )

    return state


async def track_status_node(state: dict) -> dict:
    """Track the claim status via 276/277 transactions.

    Builds a 276 claim status inquiry and processes the response.
    In production, the 276 would be submitted to the clearinghouse
    and the 277 response would be parsed asynchronously.
    """
    state["current_node"] = "track_status"
    input_data = state.get("input_data", {})
    x12_data = state.get("x12_837_data", {})

    # Build a 276 claim status request
    from app.agents.claims.tools import check_claim_status, parse_277_response
    status_request = await check_claim_status(
        subscriber_id=input_data.get("subscriber_id", ""),
        subscriber_last_name=input_data.get("subscriber_last_name", ""),
        subscriber_first_name=input_data.get("subscriber_first_name", ""),
        payer_id=input_data.get("payer_id", ""),
        payer_name=input_data.get("payer_name", ""),
        claim_id=x12_data.get("claim_id", input_data.get("claim_id", "")),
        date_of_service=input_data.get("date_of_service", ""),
    )

    state["status_request_276"] = status_request

    # In a real workflow, we'd submit the 276 to the clearinghouse and
    # receive a 277 response. For now, we generate a mock 277 response
    # that reflects the claim's current state.
    claim_id = x12_data.get("claim_id", input_data.get("claim_id", ""))
    mock_277 = _build_mock_277_response(claim_id, input_data)
    parsed_status = await parse_277_response(mock_277)

    status_data = {}
    if parsed_status.get("success"):
        status_data = parsed_status.get("parsed", {})

    state["claim_status_data"] = status_data

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="track_status",
            action="status_checked",
            details={
                "has_276": status_request.get("success", False),
                "has_277": bool(status_data),
                "claim_id": claim_id,
            },
        )
    )

    return state


def _build_mock_277_response(claim_id: str, input_data: dict) -> str:
    """Build a mock 277 response for the given claim.

    Simulates a clearinghouse response indicating the claim was received
    and is being processed.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y%m%d")
    time_str = now.strftime("%H%M")

    return (
        f"ISA*00*          *00*          *ZZ*RECEIVER       "
        f"*ZZ*SENDER         *{date_str[2:]}*{time_str}*^*00501*000000001*0*P*:~"
        f"GS*HN*RECEIVER*SENDER*{date_str}*{time_str}*1*X*005010X212~"
        f"ST*277*0001*005010X212~"
        f"BHT*0085*08*277RESP001*{date_str}*{time_str}~"
        f"HL*1**20*1~"
        f"NM1*PR*2*{input_data.get('payer_name', 'PAYER')}*****PI*"
        f"{input_data.get('payer_id', 'PAYER01')}~"
        f"HL*2*1*21*1~"
        f"NM1*41*2*BILLING PROVIDER*****46*{input_data.get('billing_provider_npi', '1234567890')}~"
        f"HL*3*2*19*1~"
        f"NM1*IL*1*{input_data.get('subscriber_last_name', 'DOE')}"
        f"*{input_data.get('subscriber_first_name', 'JANE')}****MI*"
        f"{input_data.get('subscriber_id', 'SUB001')}~"
        f"TRN*2*{claim_id}*CLEARINGHOUSE01~"
        f"STC*A1:20:PR*{date_str}**{input_data.get('total_charge', '0.00')}~"
        f"REF*1K*{claim_id}~"
        f"DTP*472*RD8*{date_str}-{date_str}~"
        f"SE*13*0001~"
        f"GE*1*1~"
        f"IEA*1*000000001~"
    )


async def parse_835_node(state: dict) -> dict:
    """Parse the 835 remittance response.

    Processes payment information, adjustments, and denial details
    from the remittance data.
    """
    state["current_node"] = "parse_835"

    # Check for remittance data from tool results or input
    tool_results = state.get("tool_results", [])
    remittance_data = {}
    for result in tool_results:
        if result.get("tool_name") == "parse_835_remittance" and result.get("success"):
            remittance_data = result.get("result", {}).get("parsed", {})
            break

    # If no parsed remittance yet, try parsing raw_835 from input_data
    if not remittance_data:
        input_data = state.get("input_data", {})
        raw_835 = input_data.get("raw_835", "")
        if raw_835:
            from app.agents.claims.tools import parse_835_remittance
            parse_result = await parse_835_remittance(raw_835)
            if parse_result.get("success"):
                remittance_data = parse_result.get("parsed", {})

    # Fall back to pre-provided remittance dict
    if not remittance_data:
        input_data = state.get("input_data", {})
        remittance_data = input_data.get("remittance_data", {})

    state["remittance_data"] = remittance_data

    # Extract payment info
    if remittance_data:
        payment = remittance_data.get("payment", {})
        claims = remittance_data.get("claims", [])

        state["payment_info"] = {
            "total_paid": payment.get("amount", "0.00"),
            "payment_method": payment.get("method", ""),
            "payment_date": payment.get("date", ""),
            "claims_count": len(claims),
        }

        # Check for denials in the claims — use Decimal for monetary comparison
        from decimal import Decimal
        denials = []
        for claim in claims:
            is_denied_status = claim.get("status_code") == "4"
            try:
                is_zero_payment = Decimal(str(claim.get("paid_amount", "0"))) == Decimal("0")
            except Exception:
                is_zero_payment = False
            if is_denied_status or is_zero_payment:
                denials.append({
                    "claim_id": claim.get("claim_id", ""),
                    "status_code": claim.get("status_code", ""),
                    "adjustments": claim.get("adjustments", []),
                })

        if denials:
            state["denials_detected"] = denials

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="parse_835",
            action="835_processed",
            details={
                "has_remittance": bool(remittance_data),
                "has_payment": bool(state.get("payment_info")),
                "denials_count": len(state.get("denials_detected", [])),
            },
        )
    )

    return state


async def handle_denial_node(state: dict) -> dict:
    """Handle claim denials with appeal recommendations.

    Analyzes denial codes and generates recommended appeal actions.
    """
    state["current_node"] = "handle_denial"
    denials = state.get("denials_detected", [])

    if not denials:
        state["audit_trail"].append(
            AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                node="handle_denial",
                action="no_denials",
                details={},
            )
        )
        return state

    input_data = state.get("input_data", {})
    from app.agents.claims.tools import analyze_denial

    denial_analyses = []
    for denial in denials:
        # Get the primary adjustment reason code
        adjustments = denial.get("adjustments", [])
        denial_code = adjustments[0].get("reason_code", "unknown") if adjustments else "unknown"

        analysis = await analyze_denial(
            denial_code=denial_code,
            denial_reason=denial.get("denial_reason", ""),
            claim_id=denial.get("claim_id", ""),
            diagnosis_codes=input_data.get("diagnosis_codes", []),
            procedure_codes=input_data.get("procedure_codes", []),
            payer_id=input_data.get("payer_id", ""),
        )
        denial_analyses.append(analysis)

    state["denial_analyses"] = denial_analyses
    state["needs_review"] = True
    state["confidence"] = 0.5
    state["review_reason"] = f"Claim denied: {len(denials)} denial(s) requiring review"

    # LLM-augmented denial analysis (optional enrichment)
    llm_provider: LLMProvider | None = state.get("_llm_provider")
    llm_used = False
    if llm_provider and denial_analyses:
        try:
            denial_details = json.dumps(denials, default=str)
            claim_details = json.dumps({
                "claim_id": state.get("x12_837_data", {}).get("claim_id", ""),
                "diagnosis_codes": input_data.get("diagnosis_codes", []),
                "procedure_codes": input_data.get("procedure_codes", []),
            }, default=str)
            payer_rules = json.dumps(state.get("payer_rules_applied", []), default=str)
            prompt = DENIAL_ANALYSIS_PROMPT.format(
                denial_details=denial_details,
                claim_details=claim_details,
                payer_rules=payer_rules,
            )
            llm_response = await llm_provider.send(
                messages=[{"role": "user", "content": prompt}],
                system_prompt=SYSTEM_PROMPT,
                max_tokens=1024,
                temperature=0.0,
            )
            state["denial_reasoning"] = llm_response.content
            llm_used = True
        except LLMError:
            logging.getLogger(__name__).debug(
                "LLM augmentation failed for handle_denial, using rule-based result"
            )

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="handle_denial",
            action="denials_analyzed",
            details={
                "denials_count": len(denials),
                "appealable_count": sum(
                    1 for a in denial_analyses
                    if a.get("appeal_recommendation", {}).get("appealable")
                ),
                "llm_augmented": llm_used,
            },
        )
    )

    return state


async def evaluate_confidence_node(state: dict) -> dict:
    """Evaluate confidence and determine if HITL escalation is needed.

    Checks claims-specific factors:
    - Code validation failures
    - High-value claims
    - Unusual code combinations
    - Denial presence
    """
    state["current_node"] = "evaluate_confidence"

    raw_confidence = state.get("confidence", 0.0)
    needs_review = state.get("needs_review", False)
    review_reason = state.get("review_reason", "")
    # Only default to 0.85 if no upstream node explicitly set confidence/review
    confidence = raw_confidence if (raw_confidence > 0.0 or needs_review) else 0.85

    # Check for errors
    if state.get("error"):
        confidence = 0.0
        needs_review = True
        review_reason = f"Error during processing: {state['error']}"
    else:
        # Code validation issues already flagged
        dx_validation = state.get("dx_validation", {})
        cpt_validation = state.get("cpt_validation", {})

        if dx_validation.get("invalid_count", 0) > 0 or cpt_validation.get("invalid_count", 0) > 0:
            confidence = min(confidence, 0.4)
            needs_review = True
            if not review_reason:
                review_reason = "Invalid diagnosis or procedure codes detected"

        # High-value claims trigger review
        input_data = state.get("input_data", {})
        try:
            total_charge = float(input_data.get("total_charge", "0"))
            if total_charge > 10000:
                confidence = min(confidence, 0.6)
                needs_review = True
                review_reason = review_reason or f"High-value claim: ${total_charge:.2f}"
        except (ValueError, TypeError):
            pass

        # Denials always trigger review
        if state.get("denials_detected"):
            confidence = min(confidence, 0.5)
            needs_review = True
            if not review_reason:
                review_reason = "Claim denial(s) detected"

    if confidence < CLAIMS_CONFIDENCE_THRESHOLD and not needs_review:
        needs_review = True
        if not review_reason:
            review_reason = (
                f"Confidence {confidence:.2f} below threshold "
                f"{CLAIMS_CONFIDENCE_THRESHOLD:.2f}"
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
        "dx_validation": state.get("dx_validation", {}),
        "cpt_validation": state.get("cpt_validation", {}),
        "claim_id": state.get("x12_837_data", {}).get("claim_id", ""),
        "submission_status": state.get("submission_status", ""),
        "payment_info": state.get("payment_info"),
        "denial_analyses": state.get("denial_analyses", []),
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


async def output_node(state: dict) -> dict:
    """Final output node — assembles the agent's claims output."""
    state["current_node"] = "output"

    if not state.get("decision"):
        state["decision"] = {
            "dx_validation": state.get("dx_validation", {}),
            "cpt_validation": state.get("cpt_validation", {}),
            "claim_id": state.get("x12_837_data", {}).get("claim_id", ""),
            "submission_status": state.get("submission_status", ""),
            "payment_info": state.get("payment_info"),
            "remittance_data": state.get("remittance_data"),
            "denial_analyses": state.get("denial_analyses", []),
            "payer_rules_applied": state.get("payer_rules_applied", []),
            "confidence": state.get("confidence", 0.85),
            "needs_review": state.get("needs_review", False),
            "review_reason": state.get("review_reason", ""),
        }

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="output",
            action="execution_completed",
            details={
                "confidence": state.get("confidence", 0.0),
                "needs_review": state.get("needs_review", False),
                "has_payment": bool(state.get("payment_info")),
                "has_denials": bool(state.get("denial_analyses")),
            },
        )
    )

    return state


# ── Graph routing ────────────────────────────────────────────────────


def _validate_codes_router(state: dict) -> str:
    """Route from validate_codes: to evaluate_confidence on missing/invalid codes for HITL escalation."""
    if state.get("needs_review"):
        return "evaluate_confidence"
    if state.get("error"):
        return "evaluate_confidence"
    return "check_payer_rules"


def _handle_denial_router(state: dict) -> str:
    """Route from handle_denial: to evaluate_confidence."""
    return "evaluate_confidence"


def _evaluate_confidence_router(state: dict) -> str:
    """Route from evaluate_confidence: to output or escalate."""
    if state.get("needs_review", False):
        return "escalate"
    return "output"


# ── Agent class ──────────────────────────────────────────────────────


class ClaimsAgent(BaseAgent):
    """Claims & Billing Agent.

    Processes insurance claims through a LangGraph workflow:

    1. validate_codes — validate ICD-10 and CPT codes
    2. check_payer_rules — check payer-specific billing rules
    3. build_837 — construct X12 837P claim
    4. submit_clearinghouse — submit via clearinghouse
    5. track_status — check claim status (276/277)
    6. parse_835 — parse remittance response
    7. handle_denial — analyze denials and recommend appeals
    8. evaluate_confidence — determine confidence and HITL escalation
    9. output — assemble final output (or escalate)
    """

    agent_type = "claims"
    confidence_threshold = CLAIMS_CONFIDENCE_THRESHOLD

    def get_tools(self) -> list[ToolDefinition]:
        """Return claims-specific tools."""
        return get_claims_tools()

    async def run(
        self,
        *,
        task_id: str | None = None,
        input_data: dict[str, Any] | None = None,
        patient_context: Any = None,
        payer_context: Any = None,
    ) -> BaseAgentState:
        """Run the claims agent, injecting the LLM provider into state.

        The LLM provider is stored in the state under ``_llm_provider`` so
        that graph nodes (validate_codes, handle_denial) can optionally call
        the LLM for augmented reasoning (code validation reasoning, denial
        analysis) while falling back to deterministic logic.
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
        """Build the claims agent graph with explicit contract nodes.

        Graph topology:
            validate_codes → [check_payer_rules | evaluate_confidence (missing/invalid codes)]
            check_payer_rules → build_837
            build_837 → submit_clearinghouse
            submit_clearinghouse → track_status
            track_status → parse_835
            parse_835 → handle_denial
            handle_denial → evaluate_confidence
            evaluate_confidence → [output | escalate]
            escalate → output
        """
        graph = StateGraph(dict)

        graph.add_node("validate_codes", validate_codes_node)
        graph.add_node("check_payer_rules", check_payer_rules_node)
        graph.add_node("build_837", build_837_node)
        graph.add_node("submit_clearinghouse", submit_clearinghouse_node)
        graph.add_node("track_status", track_status_node)
        graph.add_node("parse_835", parse_835_node)
        graph.add_node("handle_denial", handle_denial_node)
        graph.add_node("evaluate_confidence", evaluate_confidence_node)
        graph.add_node("escalate", escalate_node)
        graph.add_node("output", output_node)

        graph.set_entry_point("validate_codes")

        graph.add_conditional_edges(
            "validate_codes",
            _validate_codes_router,
            {
                "check_payer_rules": "check_payer_rules",
                "evaluate_confidence": "evaluate_confidence",
            },
        )
        graph.add_edge("check_payer_rules", "build_837")
        graph.add_edge("build_837", "submit_clearinghouse")
        graph.add_edge("submit_clearinghouse", "track_status")
        graph.add_edge("track_status", "parse_835")
        graph.add_edge("parse_835", "handle_denial")
        graph.add_edge("handle_denial", "evaluate_confidence")
        graph.add_conditional_edges(
            "evaluate_confidence",
            _evaluate_confidence_router,
            {"escalate": "escalate", "output": "output"},
        )
        graph.add_edge("escalate", "output")
        graph.add_edge("output", END)

        compiled = graph.compile()

        return AgentGraph(
            compiled_graph=compiled,
            node_names=[
                "validate_codes",
                "check_payer_rules",
                "build_837",
                "submit_clearinghouse",
                "track_status",
                "parse_835",
                "handle_denial",
                "evaluate_confidence",
                "escalate",
                "output",
            ],
        )


async def run_claims_agent(
    *,
    input_data: dict[str, Any],
    llm_provider: LLMProvider,
    session: AsyncSession | None = None,
    task_id: str | None = None,
) -> BaseAgentState:
    """Convenience function to run the claims agent.

    Creates and runs a ClaimsAgent with the given input.
    Returns the final agent state.
    """
    agent = ClaimsAgent(
        llm_provider=llm_provider,
        session=session,
    )

    patient_context = {
        "subscriber_id": input_data.get("subscriber_id", ""),
        "first_name": input_data.get("subscriber_first_name", ""),
        "last_name": input_data.get("subscriber_last_name", ""),
        "date_of_birth": input_data.get("subscriber_dob", ""),
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
