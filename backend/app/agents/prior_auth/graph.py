"""Prior Authorization agent LangGraph implementation.

Defines the complete graph with explicit PA-specific nodes:
  check_pa_required → gather_clinical_docs → build_pa_request →
  determine_submission_channel → submit_pa → track_status →
  handle_denial → generate_appeal → evaluate_confidence → output/escalate

This is the most complex agent — it handles the full PA lifecycle including
requirement determination, clinical evidence gathering, multi-channel
submission, status tracking, denial handling, and appeal letter generation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from langgraph.graph import StateGraph, END
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import BaseAgent
from app.agents.prior_auth.prompts import SYSTEM_PROMPT
from app.agents.prior_auth.tools import get_prior_auth_tools
from app.core.engine.graph_builder import AgentGraph
from app.core.engine.llm_provider import LLMProvider
from app.core.engine.state import AuditEntry, BaseAgentState, create_initial_state
from app.core.engine.tool_executor import ToolDefinition, ToolExecutor

logger = logging.getLogger(__name__)

# Confidence threshold for HITL escalation
PA_CONFIDENCE_THRESHOLD = 0.7


# ── Prior Auth graph nodes ─────────────────────────────────────────


async def check_pa_required_node(state: dict) -> dict:
    """Determine if prior authorization is required.

    Checks the payer rules for the procedure code + payer combination.
    Routes to output (no PA needed) or continues the pipeline.
    """
    state["current_node"] = "check_pa_required"
    input_data = state.get("input_data", {})

    procedure_code = input_data.get("procedure_code", "")
    payer_id = input_data.get("payer_id", "")
    diagnosis_codes = input_data.get("diagnosis_codes", [])

    if not procedure_code:
        state["error"] = "Missing required field: procedure_code"
        state["confidence"] = 0.0
        state["audit_trail"].append(
            AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                node="check_pa_required",
                action="validation_failed",
                details={"error": "missing_procedure_code"},
            )
        )
        return state

    # Check if PA is required using the tool.
    # Pass db_session from state so the payer rule engine is used when available.
    from app.agents.prior_auth.tools import check_pa_required
    db_session = state.get("db_session")
    pa_check = await check_pa_required(
        procedure_code=procedure_code,
        payer_id=payer_id,
        diagnosis_codes=diagnosis_codes,
        db_session=db_session,
    )

    state["pa_required"] = pa_check.get("pa_required", True)
    state["pa_check_result"] = pa_check
    state["clinical_docs_needed"] = pa_check.get("clinical_docs_needed", [])

    # Enrich patient context
    state["patient_context"] = {
        "patient_id": input_data.get("patient_id", ""),
        "first_name": input_data.get("subscriber_first_name", ""),
        "last_name": input_data.get("subscriber_last_name", ""),
        "date_of_birth": input_data.get("subscriber_dob", ""),
        "insurance_member_id": input_data.get("subscriber_id", ""),
    }

    # Enrich payer context
    state["payer_context"] = {
        "payer_id": payer_id,
        "payer_name": input_data.get("payer_name", ""),
    }

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="check_pa_required",
            action="pa_requirement_checked",
            details={
                "procedure_code": procedure_code,
                "payer_id": payer_id,
                "pa_required": state["pa_required"],
                "reason": pa_check.get("reason", ""),
            },
        )
    )

    return state


async def gather_clinical_docs_node(state: dict) -> dict:
    """Gather relevant clinical documentation from FHIR.

    Retrieves conditions, medications, lab results, and recent procedures
    for the patient to support the PA request.
    """
    state["current_node"] = "gather_clinical_docs"
    input_data = state.get("input_data", {})
    patient_id = input_data.get("patient_id", "")
    fhir_base_url = input_data.get("fhir_base_url", "")
    auth_token = input_data.get("fhir_auth_token", "")

    from app.agents.prior_auth.tools import gather_clinical_documents
    clinical_docs = await gather_clinical_documents(
        patient_id=patient_id,
        fhir_base_url=fhir_base_url,
        auth_token=auth_token,
    )

    state["clinical_evidence"] = clinical_docs
    state["clinical_summary"] = {
        "conditions_count": len(clinical_docs.get("conditions", [])),
        "medications_count": len(clinical_docs.get("medications", [])),
        "lab_results_count": len(clinical_docs.get("lab_results", [])),
        "procedures_count": len(clinical_docs.get("recent_procedures", [])),
        "total_documents": clinical_docs.get("document_count", 0),
    }

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="gather_clinical_docs",
            action="clinical_docs_gathered",
            details={
                "patient_id": patient_id,
                "fhir_used": bool(fhir_base_url),
                **state["clinical_summary"],
            },
        )
    )

    return state


async def build_pa_request_node(state: dict) -> dict:
    """Build the PA request data structure (X12 278 and/or Da Vinci PAS).

    Constructs the 278 request with clinical information from the gathered
    evidence. Also builds the Da Vinci PAS FHIR format for future compatibility.
    """
    state["current_node"] = "build_pa_request"
    input_data = state.get("input_data", {})

    # Build X12 278 request with clinical evidence attachment
    clinical_evidence = state.get("clinical_evidence", {})
    from app.agents.prior_auth.tools import build_278_request
    x12_result = await build_278_request(
        provider_npi=input_data.get("provider_npi", "0000000000"),
        provider_name=input_data.get("provider_name", "Provider"),
        subscriber_id=input_data.get("subscriber_id", ""),
        subscriber_first_name=input_data.get("subscriber_first_name", ""),
        subscriber_last_name=input_data.get("subscriber_last_name", ""),
        subscriber_dob=input_data.get("subscriber_dob", "19900101"),
        payer_id=input_data.get("payer_id", ""),
        payer_name=input_data.get("payer_name", ""),
        procedure_code=input_data.get("procedure_code", ""),
        diagnosis_codes=input_data.get("diagnosis_codes", []),
        date_of_service=input_data.get("date_of_service", ""),
        place_of_service=input_data.get("place_of_service", "11"),
        clinical_evidence=clinical_evidence,
    )

    state["x12_278_result"] = x12_result

    # Also build Da Vinci PAS format for future compatibility
    from app.agents.prior_auth.tools import build_davinci_pas_request
    pas_result = await build_davinci_pas_request(
        patient_id=input_data.get("patient_id", ""),
        provider_npi=input_data.get("provider_npi", ""),
        payer_id=input_data.get("payer_id", ""),
        procedure_code=input_data.get("procedure_code", ""),
        diagnosis_codes=input_data.get("diagnosis_codes", []),
        date_of_service=input_data.get("date_of_service", ""),
        clinical_info=state.get("clinical_evidence"),
    )

    state["davinci_pas_result"] = pas_result

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="build_pa_request",
            action="pa_request_built",
            details={
                "x12_278_success": x12_result.get("success", False),
                "davinci_pas_success": pas_result.get("success", False),
                "control_number": x12_result.get("control_number", ""),
            },
        )
    )

    return state


async def determine_submission_channel_node(state: dict) -> dict:
    """Determine the best submission channel for this PA request.

    Options: clearinghouse (X12 278), payer API, portal (RPA), or manual.
    Selection is based on payer configuration and available channels.
    """
    state["current_node"] = "determine_submission_channel"
    input_data = state.get("input_data", {})

    # Determine the best submission channel based on payer config
    clearinghouse_config = input_data.get("clearinghouse_config")
    submission_channel = input_data.get("submission_channel", "")
    payer_id = input_data.get("payer_id", "")

    # Normalize channel aliases: "api" is a synonym for "payer_api"
    CHANNEL_ALIASES = {"api": "payer_api"}
    VALID_CHANNELS = {"clearinghouse", "payer_api", "portal", "manual", ""}
    submission_channel = CHANNEL_ALIASES.get(submission_channel, submission_channel)

    if submission_channel and submission_channel not in VALID_CHANNELS:
        logger.warning(
            "Unknown submission_channel '%s' — defaulting to clearinghouse",
            submission_channel,
        )
        submission_channel = "clearinghouse"

    if not submission_channel:
        if clearinghouse_config:
            submission_channel = "clearinghouse"
        elif payer_id:
            # Check if payer supports direct API submission from payer rules
            pa_check = state.get("pa_check_result", {})
            payer_api_available = pa_check.get("payer_api_available", False)
            if payer_api_available:
                submission_channel = "payer_api"
            else:
                submission_channel = "clearinghouse"  # Default
        else:
            submission_channel = "clearinghouse"  # Default

    state["submission_channel"] = submission_channel

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="determine_submission_channel",
            action="submission_channel_determined",
            details={
                "channel": submission_channel,
                "payer_id": input_data.get("payer_id", ""),
            },
        )
    )

    return state


async def submit_pa_node(state: dict) -> dict:
    """Submit the PA request via the determined channel.

    Handles clearinghouse (X12 278), portal (RPA stub), or records
    the submission metadata for the Temporal workflow to execute.
    """
    state["current_node"] = "submit_pa"
    input_data = state.get("input_data", {})
    channel = state.get("submission_channel", "clearinghouse")

    if channel == "portal":
        # RPA portal submission (stub)
        from app.agents.prior_auth.rpa_stub import submit_via_portal
        portal_result = await submit_via_portal(
            payer_id=input_data.get("payer_id", ""),
            patient_name=f"{input_data.get('subscriber_first_name', '')} "
                         f"{input_data.get('subscriber_last_name', '')}",
            patient_dob=input_data.get("subscriber_dob", ""),
            subscriber_id=input_data.get("subscriber_id", ""),
            procedure_code=input_data.get("procedure_code", ""),
            diagnosis_codes=input_data.get("diagnosis_codes", []),
        )
        state["submission_result"] = portal_result

        # If portal fails, fall back to clearinghouse
        if not portal_result.get("success"):
            logger.warning(
                "Portal submission failed for payer %s, falling back to clearinghouse",
                input_data.get("payer_id", ""),
            )
            channel = "clearinghouse"
            state["submission_channel"] = channel
            # Fall through to clearinghouse below

    if channel == "payer_api":
        # Direct payer API submission
        from app.agents.prior_auth.tools import submit_pa_via_payer_api
        api_result = await submit_pa_via_payer_api(
            payer_id=input_data.get("payer_id", ""),
            procedure_code=input_data.get("procedure_code", ""),
            diagnosis_codes=input_data.get("diagnosis_codes", []),
            subscriber_id=input_data.get("subscriber_id", ""),
            subscriber_first_name=input_data.get("subscriber_first_name", ""),
            subscriber_last_name=input_data.get("subscriber_last_name", ""),
            provider_npi=input_data.get("provider_npi", ""),
            clinical_evidence=state.get("clinical_evidence", {}),
            davinci_pas_request=state.get("davinci_pas_result", {}),
        )
        state["submission_result"] = api_result

        # If payer API fails, fall back to clearinghouse
        if not api_result.get("success"):
            logger.warning(
                "Payer API submission failed for payer %s, falling back to clearinghouse",
                input_data.get("payer_id", ""),
            )
            channel = "clearinghouse"
            state["submission_channel"] = channel

    if channel == "manual":
        # Manual submission — record the request details for human processing
        import uuid as _uuid
        state["submission_result"] = {
            "success": True,
            "submission_channel": "manual",
            "transaction_id": f"MANUAL-{_uuid.uuid4().hex[:12].upper()}",
            "status": "pending_manual_submission",
            "message": (
                "PA request requires manual submission. Request details have been "
                "recorded. Staff should submit via the payer portal or fax."
            ),
        }
        state["needs_review"] = True
        state["review_reason"] = (
            "Manual PA submission required — no automated channel available "
            f"for payer {input_data.get('payer_name', input_data.get('payer_id', ''))}"
        )

    if channel == "clearinghouse" and (
        "submission_result" not in state
        or not state.get("submission_result", {}).get("success", False)
    ):
        # Clearinghouse submission (default path)
        x12_result = state.get("x12_278_result", {})
        from app.agents.prior_auth.tools import submit_pa_to_clearinghouse
        submit_result = await submit_pa_to_clearinghouse(
            x12_278=x12_result.get("x12_278", ""),
            payer_id=input_data.get("payer_id", ""),
            control_number=x12_result.get("control_number", ""),
            clearinghouse_config=input_data.get("clearinghouse_config"),
        )
        state["submission_result"] = submit_result

    submission = state.get("submission_result", {})
    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="submit_pa",
            action="pa_submitted",
            details={
                "channel": channel,
                "success": submission.get("success", False),
                "transaction_id": submission.get("transaction_id", ""),
                "status": submission.get("status", ""),
            },
        )
    )

    return state


async def track_status_node(state: dict) -> dict:
    """Track the status of the submitted PA request.

    Polls for status updates. In the real system, this would be handled
    by the Temporal workflow with timer-based follow-ups.
    """
    state["current_node"] = "track_status"
    submission = state.get("submission_result", {})
    input_data = state.get("input_data", {})

    transaction_id = submission.get("transaction_id", "")
    if not transaction_id or not submission.get("success", False):
        state["pa_status"] = "submission_failed"
        state["audit_trail"].append(
            AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                node="track_status",
                action="status_check_skipped",
                details={"reason": "no_transaction_id_or_submission_failed"},
            )
        )
        return state

    from app.agents.prior_auth.tools import poll_pa_status
    status_result = await poll_pa_status(
        transaction_id=transaction_id,
        payer_id=input_data.get("payer_id", ""),
        _force_status=input_data.get("_force_status"),
    )

    state["pa_status_result"] = status_result
    state["pa_status"] = status_result.get("status", "unknown")
    state["authorization_number"] = status_result.get("authorization_number", "")

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="track_status",
            action="status_tracked",
            details={
                "transaction_id": transaction_id,
                "status": state["pa_status"],
                "authorization_number": state["authorization_number"],
            },
        )
    )

    return state


async def handle_denial_node(state: dict) -> dict:
    """Handle PA denial — analyze denial and initiate appeal process.

    This is the first node of the appeal sub-graph. It performs:
    1. Denial reason analysis — categorize and interpret the denial
    2. Evidence gap identification — determine what additional evidence is needed
    3. Clinical attachment manifest — list documents to attach to appeal

    All denials escalate to HITL review.
    """
    state["current_node"] = "handle_denial"
    pa_status = state.get("pa_status", "")
    input_data = state.get("input_data", {})
    status_result = state.get("pa_status_result", {})

    if pa_status != "denied":
        state["audit_trail"].append(
            AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                node="handle_denial",
                action="denial_handling_skipped",
                details={"pa_status": pa_status, "reason": "not_denied"},
            )
        )
        return state

    denial_reason = status_result.get("determination_reason", "Medical necessity not established")

    # Step 1: Analyze denial reason
    denial_category = _categorize_denial(denial_reason)

    # Step 2: Identify evidence gaps
    clinical_evidence = state.get("clinical_evidence", {})
    evidence_gaps = _identify_evidence_gaps(
        denial_category=denial_category,
        clinical_evidence=clinical_evidence,
        procedure_code=input_data.get("procedure_code", ""),
    )

    # Step 3: Build clinical attachment manifest
    attachment_manifest = _build_attachment_manifest(clinical_evidence, evidence_gaps)

    # Set up denial context for appeal generation
    state["denial_info"] = {
        "denial_reason": denial_reason,
        "denial_category": denial_category,
        "denial_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "authorization_number": state.get("authorization_number", ""),
        "procedure_code": input_data.get("procedure_code", ""),
        "payer_name": input_data.get("payer_name", ""),
    }
    state["evidence_gaps"] = evidence_gaps
    state["attachment_manifest"] = attachment_manifest

    # All denials requiring appeal trigger HITL escalation
    state["needs_review"] = True
    state["review_reason"] = (
        f"Prior authorization denied for procedure {input_data.get('procedure_code', '')}. "
        f"Reason: {denial_reason}. "
        f"Category: {denial_category}. "
        f"Evidence gaps: {len(evidence_gaps)}. Appeal required."
    )

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="handle_denial",
            action="denial_analyzed",
            details={
                "denial_reason": denial_reason,
                "denial_category": denial_category,
                "procedure_code": input_data.get("procedure_code", ""),
                "evidence_gaps_count": len(evidence_gaps),
                "evidence_gaps": evidence_gaps,
                "attachment_manifest_count": len(attachment_manifest),
                "appeal_triggered": True,
            },
        )
    )

    return state


async def generate_appeal_node(state: dict) -> dict:
    """Generate an appeal letter with clinical reasoning and peer-to-peer brief.

    This is the second node of the appeal sub-graph. It:
    1. Drafts the appeal letter with medical citations
    2. Attaches clinical references
    3. Generates peer-to-peer review preparation notes

    Only runs when PA status is 'denied'.
    """
    state["current_node"] = "generate_appeal"
    pa_status = state.get("pa_status", "")

    if pa_status != "denied":
        state["audit_trail"].append(
            AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                node="generate_appeal",
                action="appeal_generation_skipped",
                details={"pa_status": pa_status, "reason": "not_denied"},
            )
        )
        return state

    input_data = state.get("input_data", {})
    denial_info = state.get("denial_info", {})
    clinical_evidence = state.get("clinical_evidence", {})

    patient_name = (
        f"{input_data.get('subscriber_first_name', '')} "
        f"{input_data.get('subscriber_last_name', '')}"
    ).strip()

    # Step 1: Generate appeal letter
    from app.agents.prior_auth.tools import generate_appeal_letter
    appeal_result = await generate_appeal_letter(
        patient_name=patient_name,
        patient_dob=input_data.get("subscriber_dob", ""),
        procedure_code=input_data.get("procedure_code", ""),
        procedure_description=input_data.get("procedure_description", "Requested procedure"),
        diagnosis_codes=input_data.get("diagnosis_codes", []),
        payer_name=input_data.get("payer_name", ""),
        auth_number=denial_info.get("authorization_number", ""),
        denial_reason=denial_info.get("denial_reason", ""),
        denial_date=denial_info.get("denial_date", ""),
        clinical_evidence=clinical_evidence,
        payer_policy_reference=input_data.get("payer_policy_reference", ""),
    )

    state["appeal_result"] = appeal_result
    state["appeal_letter"] = appeal_result.get("appeal_letter", "")

    # Step 2: Generate peer-to-peer preparation brief
    from app.agents.prior_auth.tools import generate_peer_to_peer_brief
    p2p_brief = await generate_peer_to_peer_brief(
        procedure_code=input_data.get("procedure_code", ""),
        procedure_description=input_data.get("procedure_description", "Requested procedure"),
        diagnosis_codes=input_data.get("diagnosis_codes", []),
        denial_reason=denial_info.get("denial_reason", ""),
        clinical_evidence=clinical_evidence,
    )
    state["peer_to_peer_brief"] = p2p_brief

    # Step 3: Compile attachment manifest with evidence gaps
    evidence_gaps = state.get("evidence_gaps", [])
    attachment_manifest = state.get("attachment_manifest", [])
    state["appeal_package"] = {
        "appeal_letter": state["appeal_letter"],
        "peer_to_peer_brief": p2p_brief,
        "evidence_cited": appeal_result.get("evidence_cited", {}),
        "clinical_references": appeal_result.get("clinical_references", []),
        "evidence_gaps": evidence_gaps,
        "attachment_manifest": attachment_manifest,
    }

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="generate_appeal",
            action="appeal_package_generated",
            details={
                "appeal_success": appeal_result.get("success", False),
                "evidence_cited": appeal_result.get("evidence_cited", {}),
                "clinical_references": appeal_result.get("clinical_references", []),
                "has_peer_to_peer_brief": bool(p2p_brief),
                "evidence_gaps_identified": len(evidence_gaps),
                "attachments_count": len(attachment_manifest),
            },
        )
    )

    return state


# ── Appeal sub-graph helpers ────────────────────────────────────────


def _categorize_denial(denial_reason: str) -> str:
    """Categorize a denial reason into standard categories."""
    reason_lower = denial_reason.lower()

    if "medical necessity" in reason_lower or "not medically necessary" in reason_lower:
        return "medical_necessity"
    elif "experimental" in reason_lower or "investigational" in reason_lower:
        return "experimental_investigational"
    elif "documentation" in reason_lower or "insufficient" in reason_lower:
        return "insufficient_documentation"
    elif "out of network" in reason_lower or "network" in reason_lower:
        return "out_of_network"
    elif "duplicate" in reason_lower:
        return "duplicate_request"
    elif "benefit" in reason_lower or "not covered" in reason_lower:
        return "benefit_exclusion"
    elif "timely" in reason_lower or "untimely" in reason_lower:
        return "untimely_filing"
    else:
        return "other"


def _identify_evidence_gaps(
    denial_category: str,
    clinical_evidence: dict[str, Any],
    procedure_code: str,
) -> list[dict[str, str]]:
    """Identify gaps in clinical evidence that should be addressed in appeal."""
    gaps: list[dict[str, str]] = []
    conditions = clinical_evidence.get("conditions", [])
    medications = clinical_evidence.get("medications", [])
    lab_results = clinical_evidence.get("lab_results", [])
    procedures = clinical_evidence.get("recent_procedures", [])

    if denial_category in ("medical_necessity", "insufficient_documentation"):
        if not procedures:
            gaps.append({
                "type": "conservative_treatments",
                "description": "No prior conservative treatments documented. "
                               "Include records of physical therapy, medications, or other interventions tried.",
                "priority": "high",
            })
        if not conditions:
            gaps.append({
                "type": "diagnosis_documentation",
                "description": "No supporting diagnoses documented. "
                               "Include clinical notes with confirmed diagnoses.",
                "priority": "high",
            })
        if not lab_results:
            gaps.append({
                "type": "diagnostic_results",
                "description": "No lab/diagnostic results available. "
                               "Include imaging, lab, or other diagnostic results supporting need.",
                "priority": "medium",
            })
        # Check if stopped medications (failed treatments) are documented
        stopped_meds = [m for m in medications if m.get("status") == "stopped"]
        if not stopped_meds and medications:
            gaps.append({
                "type": "failed_medication_trials",
                "description": "No documentation of failed medication trials. "
                               "Include records showing medications tried and discontinued.",
                "priority": "medium",
            })

    elif denial_category == "experimental_investigational":
        gaps.append({
            "type": "clinical_guidelines",
            "description": "Include published clinical guidelines (ACR, NCCN, etc.) "
                           "supporting the procedure as standard of care.",
            "priority": "high",
        })
        gaps.append({
            "type": "peer_reviewed_evidence",
            "description": "Include references to peer-reviewed studies demonstrating "
                           "efficacy for the patient's condition.",
            "priority": "high",
        })

    return gaps


def _build_attachment_manifest(
    clinical_evidence: dict[str, Any],
    evidence_gaps: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Build a manifest of clinical documents to attach to the appeal."""
    manifest: list[dict[str, str]] = []

    conditions = clinical_evidence.get("conditions", [])
    if conditions:
        manifest.append({
            "document_type": "clinical_notes",
            "description": f"Clinical notes documenting {len(conditions)} active conditions",
            "status": "available",
        })

    medications = clinical_evidence.get("medications", [])
    if medications:
        manifest.append({
            "document_type": "medication_history",
            "description": f"Medication history with {len(medications)} medications",
            "status": "available",
        })

    lab_results = clinical_evidence.get("lab_results", [])
    if lab_results:
        manifest.append({
            "document_type": "laboratory_results",
            "description": f"Lab results including {len(lab_results)} tests",
            "status": "available",
        })

    procedures = clinical_evidence.get("recent_procedures", [])
    if procedures:
        manifest.append({
            "document_type": "procedure_records",
            "description": f"Records of {len(procedures)} prior procedures/treatments",
            "status": "available",
        })

    # Add entries for evidence gaps that need to be obtained
    for gap in evidence_gaps:
        if gap.get("priority") == "high":
            manifest.append({
                "document_type": gap.get("type", "unknown"),
                "description": gap.get("description", "Additional documentation needed"),
                "status": "needed",
            })

    return manifest


async def evaluate_confidence_node(state: dict) -> dict:
    """Evaluate confidence and determine if HITL escalation is needed.

    For prior auth, escalation triggers on:
    - PA denial (always escalate for appeal)
    - Peer-to-peer review needed
    - Low confidence in clinical evidence
    - Submission failure
    """
    state["current_node"] = "evaluate_confidence"

    pa_status = state.get("pa_status", "unknown")
    raw_confidence = state.get("confidence", 0.0)
    confidence = raw_confidence if raw_confidence > 0.0 else 0.85
    needs_review = state.get("needs_review", False)
    review_reason = state.get("review_reason", "")

    # Check for errors
    if state.get("error"):
        confidence = 0.0
        needs_review = True
        review_reason = f"Error during processing: {state['error']}"

    # PA was denied — always escalate
    elif pa_status == "denied":
        confidence = min(confidence, 0.3)
        needs_review = True
        if not review_reason:
            review_reason = "Prior authorization denied — appeal required"

    # PA was pended — moderate confidence, may need follow-up
    elif pa_status == "pended":
        confidence = min(confidence, 0.6)

    # PA was approved — high confidence
    elif pa_status == "approved":
        confidence = max(confidence, 0.9)

    # Submission failed
    elif pa_status == "submission_failed":
        confidence = min(confidence, 0.2)
        needs_review = True
        if not review_reason:
            review_reason = "PA submission failed — manual intervention required"

    # Insufficient clinical evidence
    clinical_summary = state.get("clinical_summary", {})
    if clinical_summary.get("total_documents", 0) == 0:
        confidence = min(confidence, 0.4)
        needs_review = True
        if not review_reason:
            review_reason = "Insufficient clinical documentation for PA request"

    # Low confidence from earlier processing
    if confidence < PA_CONFIDENCE_THRESHOLD:
        needs_review = True
        if not review_reason:
            review_reason = (
                f"Confidence {confidence:.2f} below threshold "
                f"{PA_CONFIDENCE_THRESHOLD:.2f}"
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
                "pa_status": pa_status,
            },
        )
    )

    return state


async def escalate_node(state: dict) -> dict:
    """Escalation node — marks the task for HITL review.

    For prior auth, escalation always occurs for:
    - Denials requiring appeal
    - Peer-to-peer review scheduling
    - Low confidence scenarios
    """
    state["current_node"] = "escalate"
    state["needs_review"] = True

    pa_status = state.get("pa_status", "unknown")
    input_data = state.get("input_data", {})

    state["decision"] = {
        "pa_status": pa_status,
        "procedure_code": input_data.get("procedure_code", ""),
        "authorization_number": state.get("authorization_number", ""),
        "submission_channel": state.get("submission_channel", ""),
        "confidence": state.get("confidence", 0.0),
        "needs_review": True,
        "review_reason": state.get("review_reason", ""),
        "escalated": True,
        "appeal_letter": state.get("appeal_letter", ""),
        "clinical_summary": state.get("clinical_summary", {}),
    }

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="escalate",
            action="escalated_to_hitl",
            details={
                "confidence": state.get("confidence", 0.0),
                "review_reason": state.get("review_reason", ""),
                "pa_status": pa_status,
                "has_appeal_letter": bool(state.get("appeal_letter")),
            },
        )
    )

    return state


async def output_node(state: dict) -> dict:
    """Final output node — assembles the agent's output."""
    state["current_node"] = "output"

    if not state.get("decision"):
        pa_status = state.get("pa_status", "unknown")
        input_data = state.get("input_data", {})
        pa_required = state.get("pa_required", True)

        state["decision"] = {
            "pa_required": pa_required,
            "pa_status": pa_status,
            "procedure_code": input_data.get("procedure_code", ""),
            "authorization_number": state.get("authorization_number", ""),
            "submission_channel": state.get("submission_channel", ""),
            "confidence": state.get("confidence", 0.85),
            "needs_review": state.get("needs_review", False),
            "review_reason": state.get("review_reason", ""),
            "clinical_summary": state.get("clinical_summary", {}),
        }

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="output",
            action="execution_completed",
            details={
                "confidence": state.get("confidence", 0.0),
                "needs_review": state.get("needs_review", False),
                "pa_status": state.get("pa_status", "unknown"),
                "pa_required": state.get("pa_required", True),
            },
        )
    )

    return state


# ── Graph routing ────────────────────────────────────────────────────


def _check_pa_required_router(state: dict) -> str:
    """Route from check_pa_required: skip to output if PA not required or on error."""
    if state.get("error"):
        return "output"
    if not state.get("pa_required", True):
        return "output"
    return "gather_clinical_docs"


def _track_status_router(state: dict) -> str:
    """Route from track_status: to handle_denial if denied, else evaluate."""
    pa_status = state.get("pa_status", "")
    if pa_status == "denied":
        return "handle_denial"
    return "evaluate_confidence"


def _evaluate_confidence_router(state: dict) -> str:
    """Route from evaluate_confidence: to output or escalate."""
    if state.get("needs_review", False):
        return "escalate"
    return "output"


# ── Agent class ──────────────────────────────────────────────────────


class PriorAuthAgent(BaseAgent):
    """Prior Authorization Agent.

    Processes prior authorization requests through a LangGraph workflow
    with the following pipeline:

    1. check_pa_required — determine if PA is needed for procedure+payer
    2. gather_clinical_docs — retrieve clinical evidence from FHIR
    3. build_pa_request — construct X12 278 and Da Vinci PAS requests
    4. determine_submission_channel — select submission method
    5. submit_pa — submit via clearinghouse, API, or portal (RPA)
    6. track_status — poll for PA determination
    7. handle_denial — process denials and set up appeal
    8. generate_appeal — create appeal letter with clinical evidence
    9. evaluate_confidence — determine if HITL review is needed
    10. output/escalate — final output or HITL escalation
    """

    agent_type = "prior_auth"
    confidence_threshold = PA_CONFIDENCE_THRESHOLD

    def get_tools(self) -> list[ToolDefinition]:
        """Return prior auth-specific tools."""
        return get_prior_auth_tools()

    def build_graph(self) -> AgentGraph:
        """Build the prior auth agent graph with explicit contract nodes.

        Graph topology:
            check_pa_required → [gather_clinical_docs | output (PA not required/error)]
            gather_clinical_docs → build_pa_request
            build_pa_request → determine_submission_channel
            determine_submission_channel → submit_pa
            submit_pa → track_status
            track_status → [handle_denial (if denied) | evaluate_confidence]
            handle_denial → generate_appeal
            generate_appeal → evaluate_confidence
            evaluate_confidence → [output | escalate]
            escalate → output
            output → END
        """
        graph = StateGraph(dict)

        # Add all prior auth-specific nodes
        graph.add_node("check_pa_required", check_pa_required_node)
        graph.add_node("gather_clinical_docs", gather_clinical_docs_node)
        graph.add_node("build_pa_request", build_pa_request_node)
        graph.add_node("determine_submission_channel", determine_submission_channel_node)
        graph.add_node("submit_pa", submit_pa_node)
        graph.add_node("track_status", track_status_node)
        graph.add_node("handle_denial", handle_denial_node)
        graph.add_node("generate_appeal", generate_appeal_node)
        graph.add_node("evaluate_confidence", evaluate_confidence_node)
        graph.add_node("escalate", escalate_node)
        graph.add_node("output", output_node)

        # Set entry point
        graph.set_entry_point("check_pa_required")

        # Add edges
        graph.add_conditional_edges(
            "check_pa_required",
            _check_pa_required_router,
            {
                "gather_clinical_docs": "gather_clinical_docs",
                "output": "output",
            },
        )
        graph.add_edge("gather_clinical_docs", "build_pa_request")
        graph.add_edge("build_pa_request", "determine_submission_channel")
        graph.add_edge("determine_submission_channel", "submit_pa")
        graph.add_edge("submit_pa", "track_status")
        graph.add_conditional_edges(
            "track_status",
            _track_status_router,
            {
                "handle_denial": "handle_denial",
                "evaluate_confidence": "evaluate_confidence",
            },
        )
        graph.add_edge("handle_denial", "generate_appeal")
        graph.add_edge("generate_appeal", "evaluate_confidence")
        graph.add_conditional_edges(
            "evaluate_confidence",
            _evaluate_confidence_router,
            {
                "escalate": "escalate",
                "output": "output",
            },
        )
        graph.add_edge("escalate", "output")
        graph.add_edge("output", END)

        compiled = graph.compile()

        return AgentGraph(
            compiled_graph=compiled,
            node_names=[
                "check_pa_required",
                "gather_clinical_docs",
                "build_pa_request",
                "determine_submission_channel",
                "submit_pa",
                "track_status",
                "handle_denial",
                "generate_appeal",
                "evaluate_confidence",
                "escalate",
                "output",
            ],
        )


async def run_prior_auth_agent(
    *,
    input_data: dict[str, Any],
    llm_provider: LLMProvider,
    session: AsyncSession | None = None,
    task_id: str | None = None,
) -> BaseAgentState:
    """Convenience function to run the prior auth agent.

    Creates and runs a PriorAuthAgent with the given input.
    Returns the final agent state.
    """
    agent = PriorAuthAgent(
        llm_provider=llm_provider,
        session=session,
    )

    patient_context = {
        "patient_id": input_data.get("patient_id", ""),
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
