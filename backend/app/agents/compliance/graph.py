"""Compliance & Reporting agent LangGraph implementation.

Defines a complete graph with compliance-specific nodes:
  identify_measures → pull_clinical_data → evaluate_measures →
  identify_gaps → generate_report → evaluate_confidence → output/escalate

This agent evaluates healthcare organizations against quality measures
(HEDIS, MIPS, CMS Stars), identifies gaps in care, and produces compliance
reports with remediation recommendations.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from langgraph.graph import StateGraph, END
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import BaseAgent
from app.agents.compliance.prompts import SYSTEM_PROMPT
from app.agents.compliance.tools import (
    HEDIS_MEASURES,
    get_compliance_tools,
    get_measure_definitions,
    pull_clinical_data,
    evaluate_measure,
    identify_gaps,
    generate_compliance_report,
)
from app.core.engine.graph_builder import AgentGraph
from app.core.engine.llm_provider import LLMProvider
from app.core.engine.state import AuditEntry, BaseAgentState, create_initial_state
from app.core.engine.tool_executor import ToolDefinition

logger = logging.getLogger(__name__)

COMPLIANCE_CONFIDENCE_THRESHOLD = 0.7


# ── Compliance-specific graph nodes ─────────────────────────────────


async def identify_measures_node(state: dict) -> dict:
    """Identify applicable quality measures for the reporting period.

    Looks up measure definitions based on the requested measure set
    and validates the reporting period.
    """
    state["current_node"] = "identify_measures"
    input_data = state.get("input_data", {})

    measure_set = input_data.get("measure_set", "HEDIS")
    reporting_period_start = input_data.get("reporting_period_start", "")
    reporting_period_end = input_data.get("reporting_period_end", "")
    organization_id = input_data.get("organization_id", "")

    if not organization_id:
        state["error"] = "organization_id is required"
        state["confidence"] = 0.0
        state["audit_trail"].append(
            AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                node="identify_measures",
                action="validation_failed",
                details={"error": "missing organization_id"},
            )
        )
        return state

    if not reporting_period_start or not reporting_period_end:
        state["error"] = "reporting_period_start and reporting_period_end are required"
        state["confidence"] = 0.0
        state["audit_trail"].append(
            AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                node="identify_measures",
                action="validation_failed",
                details={"error": "missing reporting period"},
            )
        )
        return state

    # Get measure definitions — pass DB session if available for DB-backed lookup
    measure_ids = input_data.get("measure_ids")
    db_session = state.get("db_session") or state.get("_db_session")
    result = await get_measure_definitions(measure_set, measure_ids, db_session=db_session)

    if not result.get("success", False):
        state["error"] = result.get("error", f"Failed to load measures for '{measure_set}'")
        state["confidence"] = 0.0
        state["audit_trail"].append(
            AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                node="identify_measures",
                action="measure_lookup_failed",
                details={"error": state["error"], "measure_set": measure_set},
            )
        )
        return state

    measures = result.get("measures", {})
    if not measures:
        warning = result.get("warning", "")
        state["error"] = (
            f"No measure definitions found for measure set '{measure_set}'. "
            f"{warning}"
        ).strip()
        state["confidence"] = 0.0
        state["audit_trail"].append(
            AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                node="identify_measures",
                action="no_measures_found",
                details={
                    "measure_set": measure_set,
                    "warning": warning,
                },
            )
        )
        return state

    state["measure_definitions"] = measures
    state["measure_set"] = measure_set
    state["organization_id"] = organization_id
    state["reporting_period_start"] = reporting_period_start
    state["reporting_period_end"] = reporting_period_end

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="identify_measures",
            action="measures_identified",
            details={
                "measure_set": measure_set,
                "measure_count": len(state["measure_definitions"]),
                "organization_id": organization_id,
            },
        )
    )

    return state


async def pull_clinical_data_node(state: dict) -> dict:
    """Pull clinical data from FHIR for the patient population.

    Retrieves patient demographics, conditions, procedures, and
    observations for the organization and reporting period.
    """
    state["current_node"] = "pull_clinical_data"

    result = await pull_clinical_data(
        organization_id=state.get("organization_id", ""),
        reporting_period_start=state.get("reporting_period_start", ""),
        reporting_period_end=state.get("reporting_period_end", ""),
    )

    state["clinical_data"] = result
    state["patients"] = result.get("patients", [])
    state["_clinical_data_source"] = result.get("_source", "unknown")

    # Propagate mock data warning so downstream nodes (report, audit) can flag it
    if result.get("_mock_data_warning"):
        state["_mock_data_warning"] = result["_mock_data_warning"]

    # If the clinical data pull failed (e.g. FHIR unavailable in production),
    # flag as error so the confidence node routes to HITL review.
    if not result.get("success", False):
        state["error"] = result.get("error", "Clinical data pull failed")
        state["confidence"] = 0.0

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="pull_clinical_data",
            action="clinical_data_pulled",
            details={
                "total_patients": result.get("total_patients", 0),
                "success": result.get("success", False),
                "_source": result.get("_source", "unknown"),
                **({"_mock_data_warning": result["_mock_data_warning"]}
                   if result.get("_mock_data_warning") else {}),
            },
        )
    )

    return state


async def evaluate_measures_node(state: dict) -> dict:
    """Evaluate each quality measure against the patient population.

    Runs denominator/numerator/exclusion logic for each measure
    and computes compliance rates.
    """
    state["current_node"] = "evaluate_measures"
    measures = state.get("measure_definitions", {})
    patients = state.get("patients", [])

    measure_results = []
    for measure_id, measure_def in measures.items():
        result = await evaluate_measure(measure_def, patients)
        measure_results.append(result)

    state["measure_results"] = measure_results

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="evaluate_measures",
            action="measures_evaluated",
            details={
                "measures_evaluated": len(measure_results),
                "measures_met": sum(
                    1 for r in measure_results if r.get("meets_target", False)
                ),
            },
        )
    )

    return state


async def identify_gaps_node(state: dict) -> dict:
    """Identify gaps in care across all evaluated measures.

    Aggregates non-compliant patients and prioritizes by impact.
    """
    state["current_node"] = "identify_gaps"
    measure_results = state.get("measure_results", [])

    gap_analysis = await identify_gaps(measure_results)
    state["gap_analysis"] = gap_analysis

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="identify_gaps",
            action="gaps_identified",
            details={
                "total_gaps": gap_analysis.get("total_gaps", 0),
                "measures_below_target": len(
                    gap_analysis.get("measures_below_target", [])
                ),
            },
        )
    )

    return state


async def generate_report_node(state: dict) -> dict:
    """Generate the compliance report with scores and recommendations.

    Produces a structured report including per-measure scores,
    overall compliance, gap details, and remediation recommendations.
    """
    state["current_node"] = "generate_report"

    report_result = await generate_compliance_report(
        organization_id=state.get("organization_id", ""),
        measure_set=state.get("measure_set", "HEDIS"),
        reporting_period_start=state.get("reporting_period_start", ""),
        reporting_period_end=state.get("reporting_period_end", ""),
        measure_results=state.get("measure_results", []),
        gap_analysis=state.get("gap_analysis", {}),
    )

    report = report_result.get("report", {})

    # If clinical data came from mock source, embed a prominent warning
    # in the persisted report so consumers know the data is synthetic.
    mock_warning = state.get("_mock_data_warning")
    if mock_warning:
        report["_mock_data_warning"] = mock_warning
        report["_data_source"] = "mock"

    state["compliance_report"] = report

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="generate_report",
            action="report_generated",
            details={
                "overall_score": state["compliance_report"].get("overall_score", 0.0),
                "total_measures": state["compliance_report"].get("total_measures", 0),
                "measures_met": state["compliance_report"].get("measures_met", 0),
                **({"_data_source": "mock", "_mock_data_warning": mock_warning}
                   if mock_warning else {}),
            },
        )
    )

    return state


async def evaluate_confidence_node(state: dict) -> dict:
    """Evaluate confidence based on data quality and compliance results.

    Factors: data availability, measure coverage, score reliability.
    """
    state["current_node"] = "evaluate_confidence"

    if state.get("error"):
        state["confidence"] = 0.0
        state["needs_review"] = True
        state["review_reason"] = f"Error during processing: {state['error']}"
    else:
        confidence = 0.85
        needs_review = False
        review_reason = ""

        report = state.get("compliance_report", {})
        total_measures = report.get("total_measures", 0)
        measures_not_met = report.get("measures_not_met", 0)
        overall_score = report.get("overall_score", 0.0)

        # Reduce confidence if many measures not met
        if total_measures > 0 and measures_not_met / total_measures > 0.5:
            confidence = min(confidence, 0.6)
            needs_review = True
            review_reason = f"{measures_not_met} of {total_measures} measures below target"

        # Reduce confidence for very low overall score
        if overall_score < 0.5:
            confidence = min(confidence, 0.5)
            needs_review = True
            review_reason = f"Overall compliance score {overall_score:.1%} is critically low"

        # Data quality check
        patients = state.get("patients", [])
        if len(patients) < 5:
            confidence = min(confidence, 0.4)
            needs_review = True
            review_reason = f"Small patient population ({len(patients)}) may produce unreliable scores"

        # Mock data check — reduce confidence and flag for review when
        # report was built from synthetic fallback data
        if state.get("_clinical_data_source") == "mock":
            confidence = min(confidence, 0.3)
            needs_review = True
            review_reason = (
                "Report generated from MOCK clinical data — results are synthetic "
                "and must not be used for production reporting"
            )

        if confidence < COMPLIANCE_CONFIDENCE_THRESHOLD and not needs_review:
            needs_review = True
            review_reason = (
                f"Confidence {confidence:.2f} below threshold "
                f"{COMPLIANCE_CONFIDENCE_THRESHOLD:.2f}"
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

    report = state.get("compliance_report", {})
    state["decision"] = {
        "compliance_report": report,
        "overall_score": report.get("overall_score", 0.0),
        "total_gaps": report.get("total_gaps", 0),
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
        report = state.get("compliance_report", {})
        state["decision"] = {
            "compliance_report": report,
            "overall_score": report.get("overall_score", 0.0),
            "measure_scores": report.get("measure_scores", {}),
            "total_gaps": report.get("total_gaps", 0),
            "recommendations": report.get("recommendations", []),
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
    """Route from identify_measures: on error, go to evaluate_confidence for proper error handling."""
    if state.get("error"):
        return "evaluate_confidence"
    return "pull_clinical_data"


def _pull_data_router(state: dict) -> str:
    """Route from pull_clinical_data: on error, skip to evaluate_confidence."""
    if state.get("error"):
        return "evaluate_confidence"
    return "evaluate_measures"


def _evaluate_confidence_router(state: dict) -> str:
    """Route from evaluate_confidence: to output or escalate."""
    if state.get("needs_review", False):
        return "escalate"
    return "output"


# ── Agent class ──────────────────────────────────────────────────────


class ComplianceAgent(BaseAgent):
    """Compliance & Reporting Agent.

    Evaluates healthcare organizations against quality measures:

    1. identify_measures — look up applicable measures
    2. pull_clinical_data — retrieve patient population data
    3. evaluate_measures — compute compliance rates
    4. identify_gaps — find non-compliant patients
    5. generate_report — create compliance report
    6. evaluate_confidence — determine HITL escalation
    7. output/escalate — final result
    """

    agent_type = "compliance"
    confidence_threshold = COMPLIANCE_CONFIDENCE_THRESHOLD

    def get_tools(self) -> list[ToolDefinition]:
        """Return compliance-specific tools."""
        return get_compliance_tools()

    def build_graph(self) -> AgentGraph:
        """Build the compliance agent graph."""
        graph = StateGraph(dict)

        graph.add_node("identify_measures", identify_measures_node)
        graph.add_node("pull_clinical_data", pull_clinical_data_node)
        graph.add_node("evaluate_measures", evaluate_measures_node)
        graph.add_node("identify_gaps", identify_gaps_node)
        graph.add_node("generate_report", generate_report_node)
        graph.add_node("evaluate_confidence", evaluate_confidence_node)
        graph.add_node("escalate", escalate_node)
        graph.add_node("output", output_node)

        graph.set_entry_point("identify_measures")

        graph.add_conditional_edges(
            "identify_measures",
            _parse_request_router,
            {"pull_clinical_data": "pull_clinical_data", "evaluate_confidence": "evaluate_confidence"},
        )
        graph.add_conditional_edges(
            "pull_clinical_data",
            _pull_data_router,
            {"evaluate_measures": "evaluate_measures", "evaluate_confidence": "evaluate_confidence"},
        )
        graph.add_edge("evaluate_measures", "identify_gaps")
        graph.add_edge("identify_gaps", "generate_report")
        graph.add_edge("generate_report", "evaluate_confidence")
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
                "identify_measures",
                "pull_clinical_data",
                "evaluate_measures",
                "identify_gaps",
                "generate_report",
                "evaluate_confidence",
                "escalate",
                "output",
            ],
        )


async def run_compliance_agent(
    *,
    input_data: dict[str, Any],
    llm_provider: LLMProvider,
    session: AsyncSession | None = None,
    task_id: str | None = None,
) -> BaseAgentState:
    """Convenience function to run the compliance agent."""
    agent = ComplianceAgent(
        llm_provider=llm_provider,
        session=session,
    )

    return await agent.run(
        task_id=task_id,
        input_data=input_data,
    )
