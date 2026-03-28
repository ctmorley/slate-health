"""Unit tests for the Compliance & Reporting Agent — Sprint 9."""

import pytest
import uuid

from app.agents.compliance.graph import (
    ComplianceAgent,
    run_compliance_agent,
    identify_measures_node,
    pull_clinical_data_node,
    evaluate_measures_node,
    identify_gaps_node,
    generate_report_node,
    evaluate_confidence_node,
)
from app.agents.compliance.tools import (
    HEDIS_MEASURES,
    SUPPORTED_MEASURE_SETS,
    get_measure_definitions,
    pull_clinical_data,
    evaluate_measure,
    identify_gaps,
    generate_compliance_report,
    get_compliance_tools,
)
from app.core.engine.llm_provider import LLMProvider, MockLLMBackend
from app.core.engine.state import create_initial_state


@pytest.fixture
def llm_provider():
    return LLMProvider(primary=MockLLMBackend())


@pytest.fixture
def compliance_input():
    return {
        "organization_id": str(uuid.uuid4()),
        "measure_set": "HEDIS",
        "reporting_period_start": "2025-01-01",
        "reporting_period_end": "2025-12-31",
    }


# ── Measure Definition Tests ──────────────────────────────────────


class TestMeasureDefinitions:
    @pytest.mark.asyncio
    async def test_get_all_hedis_measures(self):
        result = await get_measure_definitions("HEDIS")
        assert result["success"] is True
        assert result["count"] == 5
        assert "BCS" in result["measures"]
        assert "CDC-HBA1C" in result["measures"]
        assert "COL" in result["measures"]

    @pytest.mark.asyncio
    async def test_get_specific_measures(self):
        result = await get_measure_definitions("HEDIS", ["BCS", "COL"])
        assert result["count"] == 2
        assert "BCS" in result["measures"]
        assert "COL" in result["measures"]
        assert "CDC-HBA1C" not in result["measures"]

    def test_five_hedis_measures_seeded(self):
        """Sample measure definitions for 5 HEDIS measures are available."""
        assert len(HEDIS_MEASURES) == 5
        for mid, mdef in HEDIS_MEASURES.items():
            assert "measure_id" in mdef
            assert "name" in mdef
            assert "denominator_criteria" in mdef
            assert "numerator_criteria" in mdef
            assert "target_rate" in mdef

    @pytest.mark.asyncio
    async def test_unsupported_measure_set_returns_error(self):
        """Unsupported measure set returns explicit error."""
        result = await get_measure_definitions("INVALID_SET")
        assert result["success"] is False
        assert "Unsupported measure set" in result["error"]
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_mips_returns_measures(self):
        """MIPS returns 5 measure definitions."""
        result = await get_measure_definitions("MIPS")
        assert result["success"] is True
        assert result["count"] == 5
        assert "MIPS-236" in result["measures"]
        assert "MIPS-001" in result["measures"]

    @pytest.mark.asyncio
    async def test_cms_stars_returns_measures(self):
        """CMS_STARS returns 5 measure definitions."""
        result = await get_measure_definitions("CMS_STARS")
        assert result["success"] is True
        assert result["count"] == 5
        assert "C01" in result["measures"]
        assert "C06" in result["measures"]

    def test_supported_measure_sets(self):
        """All three measure sets are in the supported set."""
        assert "HEDIS" in SUPPORTED_MEASURE_SETS
        assert "MIPS" in SUPPORTED_MEASURE_SETS
        assert "CMS_STARS" in SUPPORTED_MEASURE_SETS


# ── Clinical Data Tests ────────────────────────────────────────────


class TestClinicalData:
    @pytest.mark.asyncio
    async def test_pull_clinical_data_returns_patients(self):
        result = await pull_clinical_data(
            organization_id="org-1",
            reporting_period_start="2025-01-01",
            reporting_period_end="2025-12-31",
        )
        assert result["success"] is True
        assert result["total_patients"] == 10
        assert len(result["patients"]) == 10

    @pytest.mark.asyncio
    async def test_patients_have_required_fields(self):
        result = await pull_clinical_data("org-1", "2025-01-01", "2025-12-31")
        for patient in result["patients"]:
            assert "patient_id" in patient
            assert "age" in patient
            assert "gender" in patient
            assert "conditions" in patient
            assert "procedures" in patient


# ── Measure Evaluation Tests ───────────────────────────────────────


class TestMeasureEvaluation:
    @pytest.mark.asyncio
    async def test_evaluate_bcs_measure(self):
        """10 patients, BCS measure → verify correct numerator/denominator."""
        clinical = await pull_clinical_data("org-1", "2025-01-01", "2025-12-31")
        patients = clinical["patients"]
        bcs = HEDIS_MEASURES["BCS"]

        result = await evaluate_measure(bcs, patients)
        assert result["success"] is True
        assert result["measure_id"] == "BCS"
        assert result["denominator"] >= 1
        assert result["numerator"] >= 0
        assert 0.0 <= result["compliance_rate"] <= 1.0

    @pytest.mark.asyncio
    async def test_evaluate_diabetes_measure(self):
        """Diabetes HbA1c testing measure evaluation."""
        clinical = await pull_clinical_data("org-1", "2025-01-01", "2025-12-31")
        patients = clinical["patients"]
        cdc = HEDIS_MEASURES["CDC-HBA1C"]

        result = await evaluate_measure(cdc, patients)
        assert result["success"] is True
        assert result["measure_id"] == "CDC-HBA1C"
        assert result["denominator"] == 3
        assert result["numerator"] == 2
        assert result["gap_count"] == 1

    @pytest.mark.asyncio
    async def test_evaluate_min_doses_dtap(self):
        """CIS-DTaP requires min_doses=4; patients with fewer don't qualify."""
        dtap = HEDIS_MEASURES["CIS-DTaP"]
        patients = [
            {
                "patient_id": "child-1",
                "age": 2,
                "gender": "male",
                "conditions": [],
                "procedures": [
                    {"code": "90700", "display": "DTaP", "date": "2024-06-01"},
                    {"code": "90700", "display": "DTaP", "date": "2024-08-01"},
                    {"code": "90700", "display": "DTaP", "date": "2024-10-01"},
                    {"code": "90700", "display": "DTaP", "date": "2025-01-01"},
                ],
            },
            {
                "patient_id": "child-2",
                "age": 2,
                "gender": "female",
                "conditions": [],
                "procedures": [
                    {"code": "90700", "display": "DTaP", "date": "2024-06-01"},
                    {"code": "90700", "display": "DTaP", "date": "2024-08-01"},
                ],
            },
        ]
        result = await evaluate_measure(dtap, patients)
        assert result["denominator"] == 2
        # child-1 has 4 doses → qualifies; child-2 has 2 → gap
        assert result["numerator"] == 1
        assert result["gap_count"] == 1
        gap = result["gap_patients"][0]
        assert gap["patient_id"] == "child-2"
        assert gap["qualifying_events"] == 2
        assert gap["required_events"] == 4

    @pytest.mark.asyncio
    async def test_evaluate_min_visits_wcv(self):
        """WCV requires min_visits=6; patients with fewer don't qualify."""
        wcv = HEDIS_MEASURES["WCV"]
        patients = [
            {
                "patient_id": "infant-1",
                "age": 1,
                "gender": "male",
                "conditions": [],
                "procedures": [
                    {"code": "99381", "display": "Well-child", "date": f"2025-0{i}-15"}
                    for i in range(1, 7)  # 6 visits
                ],
            },
            {
                "patient_id": "infant-2",
                "age": 1,
                "gender": "female",
                "conditions": [],
                "procedures": [
                    {"code": "99391", "display": "Well-child", "date": "2025-03-01"},
                    {"code": "99391", "display": "Well-child", "date": "2025-06-01"},
                ],
            },
        ]
        result = await evaluate_measure(wcv, patients)
        assert result["denominator"] == 2
        assert result["numerator"] == 1
        assert result["gap_count"] == 1
        gap = result["gap_patients"][0]
        assert gap["patient_id"] == "infant-2"
        assert gap["required_events"] == 6

    @pytest.mark.asyncio
    async def test_evaluate_lookback_filters_old_procedures(self):
        """BCS has lookback_months=24; procedures older than 2 years don't count."""
        bcs = HEDIS_MEASURES["BCS"]
        patients = [
            {
                "patient_id": "p-old",
                "age": 55,
                "gender": "female",
                "conditions": [],
                "procedures": [
                    # This mammogram is > 24 months ago
                    {"code": "77067", "display": "Mammography", "date": "2020-01-15"},
                ],
            },
            {
                "patient_id": "p-recent",
                "age": 55,
                "gender": "female",
                "conditions": [],
                "procedures": [
                    {"code": "77067", "display": "Mammography", "date": "2025-06-15"},
                ],
            },
        ]
        result = await evaluate_measure(bcs, patients, evaluation_date="2026-03-27")
        assert result["denominator"] == 2
        # p-old's mammogram is outside the 24-month window
        assert result["numerator"] == 1
        assert result["gap_count"] == 1
        assert result["gap_patients"][0]["patient_id"] == "p-old"

    @pytest.mark.asyncio
    async def test_measure_with_zero_denominator(self):
        """Measure with no eligible patients returns 0% compliance."""
        result = await evaluate_measure(
            HEDIS_MEASURES["CIS-DTaP"],
            [{"patient_id": "p-1", "age": 50, "gender": "male",
              "conditions": [], "procedures": []}],
        )
        assert result["denominator"] == 0
        assert result["compliance_rate"] == 0.0


# ── Gap Identification Tests ───────────────────────────────────────


class TestGapIdentification:
    @pytest.mark.asyncio
    async def test_identify_gaps_from_results(self):
        """3 of 10 patients non-compliant → 3 gaps identified."""
        clinical = await pull_clinical_data("org-1", "2025-01-01", "2025-12-31")
        patients = clinical["patients"]

        cdc = HEDIS_MEASURES["CDC-HBA1C"]
        result = await evaluate_measure(cdc, patients)

        gap_result = await identify_gaps([result])
        assert gap_result["success"] is True
        assert gap_result["total_gaps"] == result["gap_count"]

        for gap in gap_result["gap_details"]:
            assert "patient_id" in gap
            assert "measure_id" in gap
            assert "missing_action" in gap

    @pytest.mark.asyncio
    async def test_identify_gaps_with_remediation_priority(self):
        """Gaps from below-target measures are high priority."""
        mock_results = [
            {
                "measure_id": "TEST1",
                "measure_name": "Test Measure",
                "meets_target": False,
                "compliance_rate": 0.5,
                "target_rate": 0.8,
                "gap_patients": [
                    {"patient_id": "p-1", "missing_action": "needs test"},
                    {"patient_id": "p-2", "missing_action": "needs test"},
                    {"patient_id": "p-3", "missing_action": "needs test"},
                ],
                "gap_count": 3,
            },
        ]
        gap_result = await identify_gaps(mock_results)
        assert gap_result["total_gaps"] == 3
        assert len(gap_result["measures_below_target"]) == 1
        for gap in gap_result["gap_details"]:
            assert gap["priority"] == "high"


# ── Report Generation Tests ────────────────────────────────────────


class TestReportGeneration:
    @pytest.mark.asyncio
    async def test_report_structure(self):
        """Verify report includes all required sections."""
        mock_results = [
            {
                "measure_id": "BCS",
                "measure_name": "Breast Cancer Screening",
                "compliance_rate": 0.8,
                "target_rate": 0.74,
                "meets_target": True,
                "denominator": 10,
                "numerator": 8,
                "gap_count": 2,
                "gap_patients": [],
            },
            {
                "measure_id": "CDC-HBA1C",
                "measure_name": "Diabetes HbA1c Testing",
                "compliance_rate": 0.6,
                "target_rate": 0.86,
                "meets_target": False,
                "denominator": 5,
                "numerator": 3,
                "gap_count": 2,
                "gap_patients": [
                    {"patient_id": "p-1", "missing_action": "needs HbA1c"},
                    {"patient_id": "p-2", "missing_action": "needs HbA1c"},
                ],
            },
        ]
        gap_analysis = await identify_gaps(mock_results)

        report_result = await generate_compliance_report(
            organization_id="org-1",
            measure_set="HEDIS",
            reporting_period_start="2025-01-01",
            reporting_period_end="2025-12-31",
            measure_results=mock_results,
            gap_analysis=gap_analysis,
        )
        assert report_result["success"] is True
        report = report_result["report"]

        assert "overall_score" in report
        assert "measure_scores" in report
        assert "total_gaps" in report
        assert "gap_details" in report
        assert "recommendations" in report
        assert report["total_measures"] == 2
        assert report["measures_met"] == 1
        assert report["measures_not_met"] == 1
        assert 0.0 <= report["overall_score"] <= 1.0

    @pytest.mark.asyncio
    async def test_report_includes_recommendations(self):
        mock_results = [
            {
                "measure_id": "TEST1",
                "measure_name": "Test Measure",
                "compliance_rate": 0.5,
                "target_rate": 0.8,
                "meets_target": False,
                "denominator": 10,
                "numerator": 5,
                "gap_count": 5,
                "gap_patients": [{"patient_id": f"p-{i}", "missing_action": "needs test"} for i in range(5)],
            },
        ]
        gap_analysis = await identify_gaps(mock_results)
        report_result = await generate_compliance_report(
            "org-1", "HEDIS", "2025-01-01", "2025-12-31",
            mock_results, gap_analysis,
        )
        report = report_result["report"]
        assert len(report["recommendations"]) >= 1
        rec = report["recommendations"][0]
        assert "measure_id" in rec
        assert "action" in rec
        assert "priority" in rec


# ── Graph Node Tests ───────────────────────────────────────────────


class TestComplianceNodes:
    @pytest.mark.asyncio
    async def test_identify_measures_success(self, compliance_input):
        state = create_initial_state(
            task_id="test-1",
            agent_type="compliance",
            input_data=compliance_input,
        )
        state = await identify_measures_node(state)
        assert state.get("error") is None
        assert len(state["measure_definitions"]) == 5

    @pytest.mark.asyncio
    async def test_identify_measures_missing_org(self):
        state = create_initial_state(
            task_id="test-2",
            agent_type="compliance",
            input_data={
                "measure_set": "HEDIS",
                "reporting_period_start": "2025-01-01",
                "reporting_period_end": "2025-12-31",
            },
        )
        state = await identify_measures_node(state)
        assert "organization_id is required" in state.get("error", "")

    @pytest.mark.asyncio
    async def test_identify_measures_missing_period(self):
        state = create_initial_state(
            task_id="test-3",
            agent_type="compliance",
            input_data={
                "organization_id": "org-1",
                "measure_set": "HEDIS",
            },
        )
        state = await identify_measures_node(state)
        assert "reporting_period" in state.get("error", "")

    @pytest.mark.asyncio
    async def test_identify_measures_unsupported_set_errors(self):
        """Unsupported measure set routes to output with error."""
        state = create_initial_state(
            task_id="test-unsupported",
            agent_type="compliance",
            input_data={
                "organization_id": str(uuid.uuid4()),
                "measure_set": "INVALID_SET",
                "reporting_period_start": "2025-01-01",
                "reporting_period_end": "2025-12-31",
            },
        )
        state = await identify_measures_node(state)
        assert state.get("error") is not None
        assert "Unsupported measure set" in state["error"]

    @pytest.mark.asyncio
    async def test_identify_measures_mips_returns_measures(self):
        """MIPS is supported and returns measure definitions."""
        state = create_initial_state(
            task_id="test-mips",
            agent_type="compliance",
            input_data={
                "organization_id": str(uuid.uuid4()),
                "measure_set": "MIPS",
                "reporting_period_start": "2025-01-01",
                "reporting_period_end": "2025-12-31",
            },
        )
        state = await identify_measures_node(state)
        assert state.get("error") is None
        assert len(state["measure_definitions"]) == 5

    @pytest.mark.asyncio
    async def test_mips_full_pipeline(self, llm_provider):
        """Full pipeline with MIPS measure set runs successfully."""
        state = await run_compliance_agent(
            input_data={
                "organization_id": str(uuid.uuid4()),
                "measure_set": "MIPS",
                "reporting_period_start": "2025-01-01",
                "reporting_period_end": "2025-12-31",
            },
            llm_provider=llm_provider,
            task_id="test-mips-full",
        )
        assert state.get("error") is None
        report = state.get("compliance_report", {})
        assert report["total_measures"] == 5
        assert "overall_score" in report

    @pytest.mark.asyncio
    async def test_cms_stars_full_pipeline(self, llm_provider):
        """Full pipeline with CMS_STARS measure set runs successfully."""
        state = await run_compliance_agent(
            input_data={
                "organization_id": str(uuid.uuid4()),
                "measure_set": "CMS_STARS",
                "reporting_period_start": "2025-01-01",
                "reporting_period_end": "2025-12-31",
            },
            llm_provider=llm_provider,
            task_id="test-cms-stars-full",
        )
        assert state.get("error") is None
        report = state.get("compliance_report", {})
        assert report["total_measures"] == 5

    @pytest.mark.asyncio
    async def test_pull_clinical_data_node(self, compliance_input):
        state = create_initial_state(
            task_id="test-4",
            agent_type="compliance",
            input_data=compliance_input,
        )
        state = await identify_measures_node(state)
        state = await pull_clinical_data_node(state)
        assert len(state["patients"]) == 10

    @pytest.mark.asyncio
    async def test_evaluate_measures_node(self, compliance_input):
        state = create_initial_state(
            task_id="test-5",
            agent_type="compliance",
            input_data=compliance_input,
        )
        state = await identify_measures_node(state)
        state = await pull_clinical_data_node(state)
        state = await evaluate_measures_node(state)
        assert len(state["measure_results"]) == 5


# ── Full Agent Run Tests ───────────────────────────────────────────


class TestComplianceAgentRun:
    @pytest.mark.asyncio
    async def test_full_pipeline(self, llm_provider, compliance_input):
        """Integration test: org + period → pull data → evaluate → report."""
        state = await run_compliance_agent(
            input_data=compliance_input,
            llm_provider=llm_provider,
            task_id="test-full-1",
        )
        assert state.get("error") is None
        report = state.get("compliance_report", {})
        assert "overall_score" in report
        assert "measure_scores" in report
        assert report["total_measures"] == 5
        assert len(state.get("audit_trail", [])) >= 5

    @pytest.mark.asyncio
    async def test_agent_produces_audit_trail(self, llm_provider, compliance_input):
        state = await run_compliance_agent(
            input_data=compliance_input,
            llm_provider=llm_provider,
            task_id="test-audit-1",
        )
        audit = state.get("audit_trail", [])
        assert len(audit) >= 5
        actions = [e.get("action", "") if isinstance(e, dict) else getattr(e, "action", "") for e in audit]
        assert "measures_identified" in actions
        assert "clinical_data_pulled" in actions
        assert "measures_evaluated" in actions
        assert "gaps_identified" in actions
        assert "report_generated" in actions

    @pytest.mark.asyncio
    async def test_compliance_report_has_scores_gaps_recommendations(self, llm_provider, compliance_input):
        """Generated report includes measure scores, gaps, and recommendations."""
        state = await run_compliance_agent(
            input_data=compliance_input,
            llm_provider=llm_provider,
            task_id="test-report-1",
        )
        report = state.get("compliance_report", {})
        assert "measure_scores" in report
        assert isinstance(report["measure_scores"], dict)
        assert "total_gaps" in report or report.get("total_gaps", 0) >= 0
        assert "recommendations" in report

    @pytest.mark.asyncio
    async def test_unsupported_measure_set_produces_error(self, llm_provider):
        """Running with unsupported measure set returns error in state."""
        state = await run_compliance_agent(
            input_data={
                "organization_id": str(uuid.uuid4()),
                "measure_set": "INVALID_SET",
                "reporting_period_start": "2025-01-01",
                "reporting_period_end": "2025-12-31",
            },
            llm_provider=llm_provider,
            task_id="test-invalid-set",
        )
        assert state.get("error") is not None

    @pytest.mark.asyncio
    async def test_agent_type_is_compliance(self, llm_provider):
        agent = ComplianceAgent(llm_provider=llm_provider)
        assert agent.agent_type == "compliance"

    @pytest.mark.asyncio
    async def test_get_tools_returns_definitions(self, llm_provider):
        agent = ComplianceAgent(llm_provider=llm_provider)
        tools = agent.get_tools()
        assert len(tools) == 5
        tool_names = [t.name for t in tools]
        assert "get_measure_definitions" in tool_names
        assert "evaluate_measure" in tool_names
        assert "generate_compliance_report" in tool_names

    @pytest.mark.asyncio
    async def test_build_graph_returns_agent_graph(self, llm_provider):
        agent = ComplianceAgent(llm_provider=llm_provider)
        graph = agent.build_graph()
        assert "identify_measures" in graph.node_names
        assert "evaluate_measures" in graph.node_names
        assert "generate_report" in graph.node_names
        assert "output" in graph.node_names


# ── Workflow Registration Tests ───────────────────────────────────


class TestWorkflowRegistration:
    def test_compliance_workflow_registered_in_worker(self):
        """Verify ComplianceWorkflow is registered in the Temporal worker."""
        from app.workflows.worker import get_registered_workflows
        from app.workflows.compliance import ComplianceWorkflow

        workflows = get_registered_workflows()
        workflow_types = [w for w in workflows if w is ComplianceWorkflow]
        assert len(workflow_types) == 1, "ComplianceWorkflow must be registered"

    def test_compliance_activities_registered_in_worker(self):
        """Verify compliance activities are registered."""
        from app.workflows.worker import get_registered_activities
        from app.workflows.compliance import (
            validate_compliance_input,
            run_compliance_agent_activity,
            write_compliance_result,
        )

        activities = get_registered_activities()
        expected = {
            validate_compliance_input,
            run_compliance_agent_activity,
            write_compliance_result,
        }
        registered = set(activities)
        for act in expected:
            assert act in registered, f"Activity {act.__name__} must be registered"


# ── Schema Validation Tests ────────────────────────────────────────


class TestComplianceSchema:
    def test_valid_request(self):
        from app.schemas.compliance import ComplianceRequest
        req = ComplianceRequest(
            organization_id=str(uuid.uuid4()),
            measure_set="HEDIS",
            reporting_period_start="2025-01-01",
            reporting_period_end="2025-12-31",
        )
        assert req.measure_set == "HEDIS"

    def test_empty_org_rejected(self):
        from app.schemas.compliance import ComplianceRequest
        with pytest.raises(ValueError, match="organization_id"):
            ComplianceRequest(
                organization_id="",
                reporting_period_start="2025-01-01",
                reporting_period_end="2025-12-31",
            )

    def test_invalid_uuid_org_rejected(self):
        """Non-UUID organization_id is rejected."""
        from app.schemas.compliance import ComplianceRequest
        with pytest.raises(ValueError, match="valid UUID"):
            ComplianceRequest(
                organization_id="not-a-uuid",
                measure_set="HEDIS",
                reporting_period_start="2025-01-01",
                reporting_period_end="2025-12-31",
            )

    def test_invalid_measure_set_rejected(self):
        """Unsupported measure_set is rejected at schema level."""
        from app.schemas.compliance import ComplianceRequest
        with pytest.raises(ValueError, match="measure_set"):
            ComplianceRequest(
                organization_id=str(uuid.uuid4()),
                measure_set="INVALID",
                reporting_period_start="2025-01-01",
                reporting_period_end="2025-12-31",
            )

    def test_invalid_date_format_rejected(self):
        """Bad date format is rejected."""
        from app.schemas.compliance import ComplianceRequest
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            ComplianceRequest(
                organization_id=str(uuid.uuid4()),
                measure_set="HEDIS",
                reporting_period_start="01/01/2025",
                reporting_period_end="2025-12-31",
            )

    def test_missing_period_rejected(self):
        from app.schemas.compliance import ComplianceRequest
        with pytest.raises(ValueError):
            ComplianceRequest(
                organization_id=str(uuid.uuid4()),
                measure_set="HEDIS",
            )

    def test_mips_measure_set_accepted(self):
        """MIPS is a valid measure_set (case insensitive)."""
        from app.schemas.compliance import ComplianceRequest
        req = ComplianceRequest(
            organization_id=str(uuid.uuid4()),
            measure_set="mips",
            reporting_period_start="2025-01-01",
            reporting_period_end="2025-12-31",
        )
        assert req.measure_set == "MIPS"


# ── Quality Measure DB Model Tests ───────────────────────────────


class TestQualityMeasureModel:
    def test_model_can_be_imported(self):
        """QualityMeasureDefinition model can be imported."""
        from app.models.quality_measure import QualityMeasureDefinition
        assert QualityMeasureDefinition.__tablename__ == "quality_measure_definitions"

    def test_model_in_init_exports(self):
        """Model is exported from models package."""
        from app.models import QualityMeasureDefinition
        assert QualityMeasureDefinition is not None


# ── Regression Tests (Iteration 6 Fixes) ─────────────────────────


class TestDbSessionKeyRegression:
    """Regression: identify_measures_node used state['_db_session'] but
    BaseAgent.run() sets state['db_session'].  Verify the key is now
    correctly read so DB-backed measure lookup works when a session is
    injected via run_compliance_agent(..., session=...)."""

    @pytest.mark.asyncio
    async def test_run_compliance_agent_with_injected_session_uses_db(
        self, llm_provider, compliance_input, db_session
    ):
        """Run compliance agent with an injected DB session and a DB-only
        measure_id to verify the session is actually forwarded to
        get_measure_definitions."""
        from app.models.quality_measure import QualityMeasureDefinition

        # Seed a measure that only exists in the DB (not in HEDIS_MEASURES)
        db_measure = QualityMeasureDefinition(
            measure_id="TEST-DB-ONLY-REG",
            measure_set="HEDIS",
            name="DB-Only Regression Measure",
            description="Exists only in DB for regression testing",
            denominator_criteria={"age_min": 18, "age_max": 75},
            numerator_criteria={"procedure_codes": ["99999"]},
            exclusion_criteria={},
            target_rate=0.5,
            version="1",
            active=True,
        )
        db_session.add(db_measure)
        await db_session.flush()

        # Request only the DB-only measure
        compliance_input["measure_ids"] = ["TEST-DB-ONLY-REG"]

        state = await run_compliance_agent(
            input_data=compliance_input,
            llm_provider=llm_provider,
            session=db_session,
            task_id="test-db-session-key-regression",
        )

        # The agent must NOT error with "No measure definitions found"
        # (which was the symptom of the _db_session vs db_session mismatch)
        assert state.get("error") is None or "No measure definitions" not in (state.get("error") or ""), (
            f"DB session was not forwarded to identify_measures_node. "
            f"Got error: {state.get('error')}"
        )

        # Verify the DB-only measure was actually used
        measures = state.get("measure_definitions", {})
        assert "TEST-DB-ONLY-REG" in measures, (
            "DB-only measure should have been loaded via the injected session"
        )

    @pytest.mark.asyncio
    async def test_identify_measures_node_reads_db_session_key(self):
        """Verify identify_measures_node reads both 'db_session' and
        '_db_session' keys for backward compatibility."""
        state = create_initial_state(
            task_id="test-key-compat",
            agent_type="compliance",
            input_data={
                "organization_id": str(uuid.uuid4()),
                "measure_set": "HEDIS",
                "reporting_period_start": "2025-01-01",
                "reporting_period_end": "2025-12-31",
            },
        )
        # Set db_session key (as BaseAgent does)
        state["db_session"] = None  # None = fall back to constants

        state = await identify_measures_node(state)
        # Should work fine with in-code constants when session is None
        assert state.get("error") is None
        assert len(state.get("measure_definitions", {})) == 5


class TestMockDataWarningPropagation:
    """Regression: when mock fallback data is used, verify a warning flag
    is propagated into the report and audit trail."""

    @pytest.mark.asyncio
    async def test_mock_fallback_produces_warning_in_report(self, llm_provider, compliance_input):
        """When clinical data comes from mock source, the report must contain
        a _mock_data_warning flag."""
        import app.config

        # Force mock fallback
        original = app.config.settings.allow_mock_fallback
        original_fhir = app.config.settings.fhir_base_url
        try:
            app.config.settings.allow_mock_fallback = True
            app.config.settings.fhir_base_url = ""  # no FHIR = triggers fallback

            state = await run_compliance_agent(
                input_data=compliance_input,
                llm_provider=llm_provider,
                task_id="test-mock-warning",
            )

            report = state.get("compliance_report", {})
            assert "_mock_data_warning" in report, (
                "Report generated from mock data must contain _mock_data_warning"
            )
            assert report.get("_data_source") == "mock"
        finally:
            app.config.settings.allow_mock_fallback = original
            app.config.settings.fhir_base_url = original_fhir

    @pytest.mark.asyncio
    async def test_mock_fallback_reduces_confidence(self, llm_provider, compliance_input):
        """When mock data is used, confidence should be reduced and review flagged."""
        import app.config

        original = app.config.settings.allow_mock_fallback
        original_fhir = app.config.settings.fhir_base_url
        try:
            app.config.settings.allow_mock_fallback = True
            app.config.settings.fhir_base_url = ""

            state = await run_compliance_agent(
                input_data=compliance_input,
                llm_provider=llm_provider,
                task_id="test-mock-confidence",
            )

            assert state.get("confidence", 1.0) <= 0.3, (
                "Confidence must be reduced when using mock clinical data"
            )
            assert state.get("needs_review") is True
            assert "mock" in (state.get("review_reason") or "").lower()
        finally:
            app.config.settings.allow_mock_fallback = original
            app.config.settings.fhir_base_url = original_fhir

    @pytest.mark.asyncio
    async def test_mock_warning_in_audit_trail(self, llm_provider, compliance_input):
        """Audit trail must record that mock data was used."""
        import app.config

        original = app.config.settings.allow_mock_fallback
        original_fhir = app.config.settings.fhir_base_url
        try:
            app.config.settings.allow_mock_fallback = True
            app.config.settings.fhir_base_url = ""

            state = await run_compliance_agent(
                input_data=compliance_input,
                llm_provider=llm_provider,
                task_id="test-mock-audit",
            )

            audit = state.get("audit_trail", [])
            # Find the clinical_data_pulled entry
            data_pulled = None
            for entry in audit:
                action = entry.get("action", "") if isinstance(entry, dict) else getattr(entry, "action", "")
                if action == "clinical_data_pulled":
                    data_pulled = entry
                    break

            assert data_pulled is not None, "clinical_data_pulled audit entry must exist"
            details = data_pulled.get("details", {}) if isinstance(data_pulled, dict) else getattr(data_pulled, "details", {})
            assert details.get("_source") == "mock"
        finally:
            app.config.settings.allow_mock_fallback = original
            app.config.settings.fhir_base_url = original_fhir

    @pytest.mark.asyncio
    async def test_default_fallback_is_disabled(self):
        """Default config must have allow_mock_fallback=False."""
        # Verify the default (not the test-overridden value)
        from app.config import Settings
        fresh = Settings(_env_file=None)
        assert fresh.allow_mock_fallback is False, (
            "Production default must be allow_mock_fallback=False"
        )


# ── Issue 1: Fail-open mock-data when FHIR URL unset ─────────────


class TestFhirUrlUnsetMockFallback:
    """When fhir_base_url is empty AND allow_mock_fallback is False,
    pull_clinical_data must return success=False — not silently
    generate mock data."""

    @pytest.mark.asyncio
    async def test_no_fhir_url_no_fallback_returns_failure(self):
        """Empty fhir_base_url + allow_mock_fallback=False → explicit failure."""
        import app.config

        original_fhir = app.config.settings.fhir_base_url
        original_fallback = app.config.settings.allow_mock_fallback
        try:
            app.config.settings.fhir_base_url = ""
            app.config.settings.allow_mock_fallback = False

            result = await pull_clinical_data(
                organization_id="org-test",
                reporting_period_start="2025-01-01",
                reporting_period_end="2025-12-31",
            )
            assert result["success"] is False, (
                "Must fail when FHIR URL unset and mock fallback disabled"
            )
            assert result["_source"] == "error"
            assert result["total_patients"] == 0
            assert "FHIR base URL" in result.get("error", "")
        finally:
            app.config.settings.fhir_base_url = original_fhir
            app.config.settings.allow_mock_fallback = original_fallback

    @pytest.mark.asyncio
    async def test_no_fhir_url_with_fallback_returns_mock(self):
        """Empty fhir_base_url + allow_mock_fallback=True → mock data returned."""
        import app.config

        original_fhir = app.config.settings.fhir_base_url
        original_fallback = app.config.settings.allow_mock_fallback
        try:
            app.config.settings.fhir_base_url = ""
            app.config.settings.allow_mock_fallback = True

            result = await pull_clinical_data(
                organization_id="org-test",
                reporting_period_start="2025-01-01",
                reporting_period_end="2025-12-31",
            )
            assert result["success"] is True
            assert result["_source"] == "mock"
            assert result["total_patients"] == 10
        finally:
            app.config.settings.fhir_base_url = original_fhir
            app.config.settings.allow_mock_fallback = original_fallback


# ── Issue 2: Continuous enrollment enforcement ────────────────────


class TestContinuousEnrollment:
    """HEDIS denominator_criteria.continuous_enrollment_months must
    be enforced in evaluate_measure."""

    @pytest.mark.asyncio
    async def test_insufficient_enrollment_excluded_from_denominator(self):
        """Patients with enrollment_months < required are excluded."""
        bcs = HEDIS_MEASURES["BCS"]
        # BCS requires continuous_enrollment_months=12
        patients = [
            {
                "patient_id": "enrolled-ok",
                "age": 55,
                "gender": "female",
                "conditions": [],
                "procedures": [{"code": "77067", "display": "Mammography", "date": "2025-06-15"}],
                "continuous_enrollment": True,
                "enrollment_months": 12,
            },
            {
                "patient_id": "enrolled-short",
                "age": 55,
                "gender": "female",
                "conditions": [],
                "procedures": [{"code": "77067", "display": "Mammography", "date": "2025-06-15"}],
                "continuous_enrollment": True,
                "enrollment_months": 6,  # Less than required 12
            },
        ]
        result = await evaluate_measure(bcs, patients)
        # Only the first patient should be in the denominator
        assert result["denominator"] == 1
        assert result["numerator"] == 1

    @pytest.mark.asyncio
    async def test_continuous_enrollment_false_excluded(self):
        """Patients with continuous_enrollment=False are excluded."""
        bcs = HEDIS_MEASURES["BCS"]
        patients = [
            {
                "patient_id": "enrolled-true",
                "age": 55,
                "gender": "female",
                "conditions": [],
                "procedures": [{"code": "77067", "display": "Mammography", "date": "2025-06-15"}],
                "continuous_enrollment": True,
            },
            {
                "patient_id": "enrolled-false",
                "age": 55,
                "gender": "female",
                "conditions": [],
                "procedures": [{"code": "77067", "display": "Mammography", "date": "2025-06-15"}],
                "continuous_enrollment": False,
            },
        ]
        result = await evaluate_measure(bcs, patients)
        assert result["denominator"] == 1
        assert result["numerator"] == 1

    @pytest.mark.asyncio
    async def test_no_enrollment_field_still_included(self):
        """Patients without enrollment fields are still included (backward compat)."""
        bcs = HEDIS_MEASURES["BCS"]
        patients = [
            {
                "patient_id": "no-field",
                "age": 55,
                "gender": "female",
                "conditions": [],
                "procedures": [{"code": "77067", "display": "Mammography", "date": "2025-06-15"}],
                # No continuous_enrollment or enrollment_months fields
            },
        ]
        result = await evaluate_measure(bcs, patients)
        assert result["denominator"] == 1

    @pytest.mark.asyncio
    async def test_enrollment_months_zero_means_no_check(self):
        """Measures without continuous_enrollment_months don't filter on enrollment."""
        # Create a custom measure with no enrollment requirement
        custom_measure = {
            "measure_id": "TEST-NO-ENROLL",
            "name": "Test Measure Without Enrollment",
            "denominator_criteria": {
                "age_min": 18,
                "age_max": 65,
                # No continuous_enrollment_months
            },
            "numerator_criteria": {
                "procedure_codes": ["99999"],
            },
            "exclusion_criteria": {},
            "target_rate": 0.5,
        }
        patients = [
            {
                "patient_id": "p-short-enroll",
                "age": 30,
                "gender": "male",
                "conditions": [],
                "procedures": [{"code": "99999", "display": "Test", "date": "2025-06-01"}],
                "enrollment_months": 3,  # Short enrollment, but measure doesn't require it
            },
        ]
        result = await evaluate_measure(custom_measure, patients)
        assert result["denominator"] == 1
        assert result["numerator"] == 1
