"""Credentialing agent LangGraph implementation.

Defines a complete graph with credentialing-specific nodes:
  lookup_provider → verify_licenses → check_sanctions → compile_application →
  submit → track_status → evaluate_confidence → output/escalate

This agent processes provider credentialing through NPPES/CAQH lookup,
license verification, sanctions checks, and application compilation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from langgraph.graph import StateGraph, END
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import BaseAgent
from app.agents.credentialing.prompts import SYSTEM_PROMPT
from app.agents.credentialing.tools import (
    REQUIRED_DOCUMENTS,
    get_credentialing_tools,
    lookup_nppes,
    query_caqh,
    verify_state_license,
    check_oig_exclusion,
    compile_application,
    submit_application,
)
from app.core.engine.graph_builder import AgentGraph
from app.core.engine.llm_provider import LLMProvider
from app.core.engine.state import AuditEntry, BaseAgentState, create_initial_state
from app.core.engine.tool_executor import ToolDefinition

logger = logging.getLogger(__name__)

CREDENTIALING_CONFIDENCE_THRESHOLD = 0.7


# ── Credentialing-specific graph nodes ──────────────────────────────


async def lookup_provider_node(state: dict) -> dict:
    """Look up provider details via NPPES and CAQH.

    Retrieves provider demographics, taxonomy, and existing
    credentialing data from NPPES and CAQH registries.
    """
    state["current_node"] = "lookup_provider"
    input_data = state.get("input_data", {})
    npi = input_data.get("provider_npi", "")

    if not npi:
        state["error"] = "provider_npi is required"
        state["confidence"] = 0.0
        state["audit_trail"].append(
            AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                node="lookup_provider",
                action="validation_failed",
                details={"error": "missing provider_npi"},
            )
        )
        return state

    # Query NPPES
    nppes_result = await lookup_nppes(npi)
    state["nppes_result"] = nppes_result

    # Query CAQH
    caqh_result = await query_caqh(npi)
    state["caqh_result"] = caqh_result

    # Build provider context
    if nppes_result.get("success"):
        state["provider_details"] = {
            "npi": npi,
            "first_name": nppes_result.get("first_name", ""),
            "last_name": nppes_result.get("last_name", ""),
            "credential": nppes_result.get("credential", ""),
            "taxonomy": nppes_result.get("taxonomy", {}),
            "addresses": nppes_result.get("addresses", []),
            "enumeration_date": nppes_result.get("enumeration_date", ""),
        }
        # Merge CAQH data if available
        if caqh_result.get("success"):
            state["provider_details"]["caqh_id"] = caqh_result.get("caqh_id", "")
            state["provider_details"]["education"] = caqh_result.get("education", [])
            state["provider_details"]["training"] = caqh_result.get("training", [])
            state["provider_details"]["documents_on_file"] = caqh_result.get(
                "documents_on_file", []
            )
    else:
        state["error"] = f"NPPES lookup failed: {nppes_result.get('error', 'Unknown error')}"
        state["confidence"] = 0.0

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="lookup_provider",
            action="provider_lookup_completed",
            details={
                "npi": npi,
                "nppes_success": nppes_result.get("success", False),
                "caqh_success": caqh_result.get("success", False),
            },
        )
    )

    return state


async def verify_licenses_node(state: dict) -> dict:
    """Verify provider licenses and certifications.

    Checks state medical license status and identifies required
    documents that are missing or expired.
    """
    state["current_node"] = "verify_licenses"
    provider_details = state.get("provider_details", {})
    npi = provider_details.get("npi", "")
    input_data = state.get("input_data", {})

    # Get state from taxonomy or input
    taxonomy = provider_details.get("taxonomy", {})
    license_state = taxonomy.get("state", input_data.get("state", "CA"))
    license_number = taxonomy.get("license", "")

    # Verify state license
    license_result = await verify_state_license(npi, license_state, license_number)
    state["license_verification"] = license_result

    # Determine required documents based on credentialing type
    cred_type = input_data.get("credentialing_type", "initial")
    required = REQUIRED_DOCUMENTS.get(cred_type, REQUIRED_DOCUMENTS["initial"])
    on_file = provider_details.get("documents_on_file", [])

    # Identify missing documents
    missing = [doc for doc in required if doc not in on_file]

    state["documents_checklist"] = {
        "required": required,
        "on_file": on_file,
        "missing": missing,
        "credentialing_type": cred_type,
    }

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="verify_licenses",
            action="licenses_verified",
            details={
                "license_verified": license_result.get("verified", False),
                "license_status": license_result.get("license_status", "unknown"),
                "required_docs": len(required),
                "missing_docs": len(missing),
            },
        )
    )

    return state


async def check_sanctions_node(state: dict) -> dict:
    """Check OIG exclusion list and SAM.gov for sanctions.

    Queries federal exclusion databases to ensure the provider
    is not barred from participating in federal healthcare programs.
    """
    state["current_node"] = "check_sanctions"
    provider_details = state.get("provider_details", {})
    npi = provider_details.get("npi", "")
    provider_name = (
        f"{provider_details.get('first_name', '')} "
        f"{provider_details.get('last_name', '')}"
    ).strip()

    sanctions_result = await check_oig_exclusion(npi, provider_name)
    state["sanctions_check"] = sanctions_result

    # Build consolidated verification results
    license_verification = state.get("license_verification", {})
    state["verification_results"] = {
        "licenses": [license_verification] if license_verification.get("success") else [],
        "sanctions_clear": (
            sanctions_result.get("success", False)
            and not sanctions_result.get("oig_excluded", True)
            and not sanctions_result.get("sam_excluded", True)
        ),
        "sanctions_details": sanctions_result,
    }

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="check_sanctions",
            action="sanctions_checked",
            details={
                "oig_excluded": sanctions_result.get("oig_excluded", False),
                "sam_excluded": sanctions_result.get("sam_excluded", False),
                "sanctions_clear": state["verification_results"]["sanctions_clear"],
            },
        )
    )

    return state


async def compile_application_node(state: dict) -> dict:
    """Compile all verified data into a credentialing application.

    Assembles provider details, verification results, and documents
    into a structured application ready for submission or HITL review.
    """
    state["current_node"] = "compile_application"
    input_data = state.get("input_data", {})

    app_result = await compile_application(
        npi=state.get("provider_details", {}).get("npi", ""),
        provider_details=state.get("provider_details", {}),
        verification_results=state.get("verification_results", {}),
        documents_checklist=state.get("documents_checklist", {}),
        target_organization=input_data.get("target_organization", ""),
        target_payer_id=input_data.get("target_payer_id", ""),
    )

    state["application"] = app_result.get("application", {})

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="compile_application",
            action="application_compiled",
            details={
                "ready_to_submit": app_result.get("ready_to_submit", False),
                "missing_documents": app_result.get("missing_documents", []),
            },
        )
    )

    return state


async def submit_node(state: dict) -> dict:
    """Submit the credentialing application if ready.

    If missing documents prevent submission, the application is
    marked for HITL review for document collection.
    """
    state["current_node"] = "submit"
    application = state.get("application", {})

    if not application.get("ready_to_submit", False):
        # Not ready — needs human review for document collection
        state["needs_review"] = True
        state["review_reason"] = (
            f"Missing required documents: {', '.join(application.get('missing_documents', []))}"
        )
        state["submission_result"] = {
            "submitted": False,
            "reason": "missing_documents",
            "missing": application.get("missing_documents", []),
        }
    else:
        submit_result = await submit_application(
            application_data=application,
            target_organization=application.get("target_organization", ""),
            target_payer_id=application.get("target_payer_id", ""),
        )
        state["submission_result"] = submit_result

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="submit",
            action="submission_attempted",
            details={
                "submitted": state["submission_result"].get("submitted", state["submission_result"].get("success", False)),
                "tracking_number": state["submission_result"].get("tracking_number", ""),
            },
        )
    )

    return state


async def track_status_node(state: dict) -> dict:
    """Track the status of the submitted application.

    Records the current tracking state and determines lifecycle status.
    Successfully submitted applications are recorded as ``submitted``
    (the initial persisted stage).  The transition to ``under_review``
    occurs later during the Temporal check-in polling loop.
    """
    state["current_node"] = "track_status"
    submission = state.get("submission_result", {})

    if submission.get("success"):
        # Record the application as submitted — the first persisted stage.
        # Transition to under_review happens during Temporal polling.
        state["application_status"] = {
            "status": "submitted",
            "tracking_number": submission.get("tracking_number", ""),
            "estimated_review_days": submission.get("estimated_review_days", 90),
            "submission_date": submission.get("submission_date", ""),
        }
    else:
        state["application_status"] = {
            "status": "pending_documents",
            "missing_documents": submission.get("missing_documents", submission.get("missing", [])),
        }

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="track_status",
            action="status_tracked",
            details={"status": state["application_status"].get("status", "")},
        )
    )

    return state


async def alert_expirations_node(state: dict) -> dict:
    """Check for upcoming credential expirations and generate alerts.

    Reviews license and certification expiration dates, generating
    warnings for credentials expiring within 90 days.
    """
    state["current_node"] = "alert_expirations"

    license_verification = state.get("license_verification", {})
    provider_details = state.get("provider_details", {})
    expiration_alerts = []

    # Check license expiration
    expiration_date_str = license_verification.get("expiration_date", "")
    if expiration_date_str:
        try:
            from datetime import date

            exp_date = date.fromisoformat(expiration_date_str)
            days_until = (exp_date - date.today()).days
            if days_until <= 90:
                expiration_alerts.append({
                    "type": "license_expiration",
                    "credential": "medical_license",
                    "state": license_verification.get("state", ""),
                    "expiration_date": expiration_date_str,
                    "days_until_expiry": days_until,
                    "severity": "critical" if days_until <= 30 else "warning",
                    "action_required": "Renew medical license before expiration",
                })
        except (ValueError, TypeError):
            pass

    # Check documents on file for expiring certifications
    documents_checklist = state.get("documents_checklist", {})
    caqh_result = state.get("caqh_result", {})
    attestation_date_str = caqh_result.get("last_attestation_date", "")
    if attestation_date_str:
        try:
            from datetime import date

            att_date = date.fromisoformat(attestation_date_str)
            # CAQH attestation is typically valid for 120 days
            att_expiry = att_date.toordinal() + 120
            days_until = att_expiry - date.today().toordinal()
            if days_until <= 90:
                expiration_alerts.append({
                    "type": "caqh_attestation",
                    "credential": "caqh_attestation",
                    "expiration_date": attestation_date_str,
                    "days_until_expiry": days_until,
                    "severity": "critical" if days_until <= 30 else "warning",
                    "action_required": "Complete CAQH re-attestation",
                })
        except (ValueError, TypeError):
            pass

    state["expiration_alerts"] = expiration_alerts

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="alert_expirations",
            action="expirations_checked",
            details={
                "alerts_count": len(expiration_alerts),
                "alerts": [
                    {"type": a["type"], "severity": a["severity"]}
                    for a in expiration_alerts
                ],
            },
        )
    )

    return state


async def evaluate_confidence_node(state: dict) -> dict:
    """Evaluate confidence and determine if HITL escalation is needed.

    Factors: NPPES lookup success, license verification, sanctions
    clear, document completeness, submission success.
    """
    state["current_node"] = "evaluate_confidence"

    if state.get("error"):
        state["confidence"] = 0.0
        state["needs_review"] = True
        state["review_reason"] = f"Error during processing: {state['error']}"
    else:
        confidence = 0.85
        needs_review = state.get("needs_review", False)
        review_reason = state.get("review_reason", "")

        # Reduce confidence for missing documents
        docs_checklist = state.get("documents_checklist", {})
        missing = docs_checklist.get("missing", [])
        if missing:
            confidence = max(0.3, confidence - 0.15 * len(missing))
            if not needs_review:
                needs_review = True
                review_reason = f"Missing {len(missing)} required document(s)"

        # Reduce confidence for sanctions
        verification = state.get("verification_results", {})
        if not verification.get("sanctions_clear", True):
            confidence = 0.1
            needs_review = True
            review_reason = "Provider has sanctions or exclusions"

        # Reduce confidence for license issues
        license_check = state.get("license_verification", {})
        if license_check.get("license_status") != "active":
            confidence = min(confidence, 0.4)
            needs_review = True
            review_reason = f"License status: {license_check.get('license_status', 'unknown')}"

        if confidence < CREDENTIALING_CONFIDENCE_THRESHOLD and not needs_review:
            needs_review = True
            review_reason = (
                f"Confidence {confidence:.2f} below threshold "
                f"{CREDENTIALING_CONFIDENCE_THRESHOLD:.2f}"
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
                "confidence": state.get("confidence", 0.0),
                "needs_review": state.get("needs_review", False),
                "review_reason": state.get("review_reason", ""),
            },
        )
    )

    return state


async def escalate_node(state: dict) -> dict:
    """Escalation node — marks the task for HITL review."""
    state["current_node"] = "escalate"
    state["needs_review"] = True

    state["decision"] = {
        "application_status": state.get("application_status", {}),
        "provider_details": {
            "npi": state.get("provider_details", {}).get("npi", ""),
            "name": (
                f"{state.get('provider_details', {}).get('first_name', '')} "
                f"{state.get('provider_details', {}).get('last_name', '')}"
            ).strip(),
        },
        "missing_documents": state.get("documents_checklist", {}).get("missing", []),
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
    """Final output node — assembles the agent's output."""
    state["current_node"] = "output"

    if not state.get("decision"):
        state["decision"] = {
            "application_status": state.get("application_status", {}),
            "provider_details": {
                "npi": state.get("provider_details", {}).get("npi", ""),
                "name": (
                    f"{state.get('provider_details', {}).get('first_name', '')} "
                    f"{state.get('provider_details', {}).get('last_name', '')}"
                ).strip(),
                "credential": state.get("provider_details", {}).get("credential", ""),
                "specialty": state.get("provider_details", {}).get("taxonomy", {}).get(
                    "description", ""
                ),
            },
            "documents_checklist": state.get("documents_checklist", {}),
            "verification_results": {
                "sanctions_clear": state.get("verification_results", {}).get(
                    "sanctions_clear", True
                ),
                "license_verified": state.get("license_verification", {}).get(
                    "verified", False
                ),
            },
            "submission_result": state.get("submission_result", {}),
            "confidence": state.get("confidence", 0.0),
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
            },
        )
    )

    return state


# ── Graph routing ────────────────────────────────────────────────────


def _parse_request_router(state: dict) -> str:
    """Route from lookup_provider: on error, go to evaluate_confidence for proper error handling."""
    if state.get("error"):
        return "evaluate_confidence"
    return "verify_licenses"


def _evaluate_confidence_router(state: dict) -> str:
    """Route from evaluate_confidence: to output or escalate."""
    if state.get("needs_review", False):
        return "escalate"
    return "output"


# ── Agent class ──────────────────────────────────────────────────────


class CredentialingAgent(BaseAgent):
    """Credentialing Agent.

    Processes provider credentialing through a LangGraph workflow:

    1. lookup_provider — NPPES + CAQH lookup
    2. verify_licenses — state license verification
    3. check_sanctions — OIG exclusion + SAM check
    4. compile_application — assemble application
    5. submit — submit or flag for HITL
    6. track_status — record tracking state
    7. alert_expirations — check for upcoming credential expirations
    8. evaluate_confidence — determine HITL escalation
    9. output/escalate — final result
    """

    agent_type = "credentialing"
    confidence_threshold = CREDENTIALING_CONFIDENCE_THRESHOLD

    def get_tools(self) -> list[ToolDefinition]:
        """Return credentialing-specific tools."""
        return get_credentialing_tools()

    def build_graph(self) -> AgentGraph:
        """Build the credentialing agent graph."""
        graph = StateGraph(dict)

        graph.add_node("lookup_provider", lookup_provider_node)
        graph.add_node("verify_licenses", verify_licenses_node)
        graph.add_node("check_sanctions", check_sanctions_node)
        graph.add_node("compile_application", compile_application_node)
        graph.add_node("submit", submit_node)
        graph.add_node("track_status", track_status_node)
        graph.add_node("alert_expirations", alert_expirations_node)
        graph.add_node("evaluate_confidence", evaluate_confidence_node)
        graph.add_node("escalate", escalate_node)
        graph.add_node("output", output_node)

        graph.set_entry_point("lookup_provider")

        graph.add_conditional_edges(
            "lookup_provider",
            _parse_request_router,
            {"verify_licenses": "verify_licenses", "evaluate_confidence": "evaluate_confidence"},
        )
        graph.add_edge("verify_licenses", "check_sanctions")
        graph.add_edge("check_sanctions", "compile_application")
        graph.add_edge("compile_application", "submit")
        graph.add_edge("submit", "track_status")
        graph.add_edge("track_status", "alert_expirations")
        graph.add_edge("alert_expirations", "evaluate_confidence")
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
                "lookup_provider",
                "verify_licenses",
                "check_sanctions",
                "compile_application",
                "submit",
                "track_status",
                "alert_expirations",
                "evaluate_confidence",
                "escalate",
                "output",
            ],
        )


async def run_credentialing_agent(
    *,
    input_data: dict[str, Any],
    llm_provider: LLMProvider,
    session: AsyncSession | None = None,
    task_id: str | None = None,
) -> BaseAgentState:
    """Convenience function to run the credentialing agent."""
    agent = CredentialingAgent(
        llm_provider=llm_provider,
        session=session,
    )

    return await agent.run(
        task_id=task_id,
        input_data=input_data,
    )
