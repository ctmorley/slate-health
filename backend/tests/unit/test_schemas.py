"""Unit tests for Sprint 3 Pydantic schemas: agent, review, payer.

Validates required fields, optional defaults, model parsing behavior,
serialization, and validation error handling.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.agent import (
    AgentStatsResponse,
    AgentTaskCreate,
    AgentTaskList,
    AgentTaskResponse,
)
from app.schemas.review import (
    ReviewActionRequest,
    ReviewList,
    ReviewResponse,
)
from app.schemas.payer import (
    PayerCreate,
    PayerResponse,
    PayerRuleCreate,
    PayerRuleEvaluationRequest,
    PayerRuleResponse,
    PayerRuleUpdate,
)


# ── Agent Schema Tests ────────────────────────────────────────────────


class TestAgentTaskCreate:
    def test_minimal_valid(self):
        task = AgentTaskCreate(agent_type="eligibility")
        assert task.agent_type == "eligibility"
        assert task.input_data == {}
        assert task.patient_id is None
        assert task.organization_id is None

    def test_full_valid(self):
        pid = uuid.uuid4()
        oid = uuid.uuid4()
        task = AgentTaskCreate(
            agent_type="claims",
            input_data={"encounter_id": "enc-1", "codes": ["99213"]},
            patient_id=pid,
            organization_id=oid,
        )
        assert task.agent_type == "claims"
        assert task.input_data["encounter_id"] == "enc-1"
        assert task.patient_id == pid
        assert task.organization_id == oid

    def test_missing_agent_type_is_optional(self):
        """agent_type is optional in the body (taken from URL path param)."""
        task = AgentTaskCreate()
        assert task.agent_type is None
        assert task.input_data == {}

    def test_input_data_default_is_empty_dict(self):
        task = AgentTaskCreate(agent_type="scheduling")
        assert task.input_data == {}
        # Ensure each instance gets its own dict (no shared mutable default)
        task2 = AgentTaskCreate(agent_type="scheduling")
        task.input_data["key"] = "value"
        assert "key" not in task2.input_data


class TestAgentTaskResponse:
    def _make_response(self, **overrides):
        defaults = {
            "id": uuid.uuid4(),
            "agent_type": "eligibility",
            "status": "completed",
            "created_at": datetime.now(timezone.utc),
        }
        defaults.update(overrides)
        return AgentTaskResponse(**defaults)

    def test_minimal_response(self):
        resp = self._make_response()
        assert resp.status == "completed"
        assert resp.output_data is None
        assert resp.error_message is None
        assert resp.confidence_score is None

    def test_full_response(self):
        now = datetime.now(timezone.utc)
        resp = self._make_response(
            output_data={"coverage": "active"},
            error_message=None,
            confidence_score=0.95,
            updated_at=now,
        )
        assert resp.output_data == {"coverage": "active"}
        assert resp.confidence_score == 0.95
        assert resp.updated_at == now

    def test_missing_required_field_raises(self):
        with pytest.raises(ValidationError):
            AgentTaskResponse(
                agent_type="eligibility",
                status="completed",
                created_at=datetime.now(timezone.utc),
                # missing 'id'
            )

    def test_from_attributes_config(self):
        assert AgentTaskResponse.model_config.get("from_attributes") is True


class TestAgentTaskList:
    def test_valid_list(self):
        item = AgentTaskResponse(
            id=uuid.uuid4(),
            agent_type="claims",
            status="running",
            created_at=datetime.now(timezone.utc),
        )
        task_list = AgentTaskList(items=[item], total=1, limit=10, offset=0)
        assert task_list.total == 1
        assert len(task_list.items) == 1
        assert task_list.items[0].agent_type == "claims"

    def test_empty_list(self):
        task_list = AgentTaskList(items=[], total=0, limit=50, offset=0)
        assert task_list.total == 0
        assert task_list.items == []


class TestAgentStatsResponse:
    def test_defaults(self):
        stats = AgentStatsResponse(agent_type="prior_auth")
        assert stats.total_tasks == 0
        assert stats.pending == 0
        assert stats.running == 0
        assert stats.completed == 0
        assert stats.failed == 0
        assert stats.in_review == 0
        assert stats.cancelled == 0
        assert stats.avg_confidence is None

    def test_full_stats(self):
        stats = AgentStatsResponse(
            agent_type="eligibility",
            total_tasks=100,
            pending=5,
            running=10,
            completed=80,
            failed=3,
            in_review=2,
            cancelled=0,
            avg_confidence=0.87,
        )
        assert stats.total_tasks == 100
        assert stats.avg_confidence == 0.87


# ── Review Schema Tests ───────────────────────────────────────────────


class TestReviewResponse:
    def _make_review(self, **overrides):
        defaults = {
            "id": uuid.uuid4(),
            "task_id": uuid.uuid4(),
            "status": "pending",
            "reason": "Low confidence",
            "created_at": datetime.now(timezone.utc),
        }
        defaults.update(overrides)
        return ReviewResponse(**defaults)

    def test_minimal_review(self):
        review = self._make_review()
        assert review.status == "pending"
        assert review.reviewer_id is None
        assert review.agent_decision is None
        assert review.confidence_score is None
        assert review.reviewer_notes is None
        assert review.decided_at is None

    def test_full_review(self):
        now = datetime.now(timezone.utc)
        reviewer_id = uuid.uuid4()
        review = self._make_review(
            reviewer_id=reviewer_id,
            agent_decision={"result": "uncertain"},
            confidence_score=0.4,
            reviewer_notes="Approved after manual check",
            decided_at=now,
            updated_at=now,
        )
        assert review.reviewer_id == reviewer_id
        assert review.confidence_score == 0.4
        assert review.reviewer_notes == "Approved after manual check"

    def test_missing_required_fields_raises(self):
        with pytest.raises(ValidationError):
            ReviewResponse(
                id=uuid.uuid4(),
                # missing task_id, status, reason, created_at
            )

    def test_from_attributes_config(self):
        assert ReviewResponse.model_config.get("from_attributes") is True


class TestReviewList:
    def test_valid_list(self):
        item = ReviewResponse(
            id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            status="pending",
            reason="Low confidence",
            created_at=datetime.now(timezone.utc),
        )
        review_list = ReviewList(items=[item], total=1, limit=10, offset=0)
        assert review_list.total == 1
        assert len(review_list.items) == 1

    def test_empty_list(self):
        review_list = ReviewList(items=[], total=0, limit=50, offset=0)
        assert review_list.items == []


class TestReviewActionRequest:
    def test_empty_request(self):
        req = ReviewActionRequest()
        assert req.notes is None

    def test_with_notes(self):
        req = ReviewActionRequest(notes="Looks correct")
        assert req.notes == "Looks correct"


# ── Payer Schema Tests ────────────────────────────────────────────────


class TestPayerCreate:
    def test_minimal_valid(self):
        payer = PayerCreate(name="Aetna", payer_id_code="AET01")
        assert payer.name == "Aetna"
        assert payer.payer_id_code == "AET01"
        assert payer.payer_type is None
        assert payer.address is None
        assert payer.phone is None
        assert payer.electronic_payer_id is None

    def test_full_valid(self):
        payer = PayerCreate(
            name="Blue Cross",
            payer_id_code="BCBS01",
            payer_type="commercial",
            address="123 Main St",
            phone="555-1234",
            electronic_payer_id="E-BCBS01",
        )
        assert payer.payer_type == "commercial"
        assert payer.electronic_payer_id == "E-BCBS01"

    def test_missing_required_raises(self):
        with pytest.raises(ValidationError):
            PayerCreate(name="Aetna")  # missing payer_id_code


class TestPayerResponse:
    def test_full_response(self):
        pid = uuid.uuid4()
        now = datetime.now(timezone.utc)
        resp = PayerResponse(
            id=pid,
            name="Aetna",
            payer_id_code="AET01",
            is_active=True,
            created_at=now,
        )
        assert resp.id == pid
        assert resp.is_active is True
        assert resp.payer_type is None

    def test_from_attributes_config(self):
        assert PayerResponse.model_config.get("from_attributes") is True


class TestPayerRuleCreate:
    def test_minimal_valid(self):
        rule = PayerRuleCreate(
            agent_type="eligibility",
            rule_type="coverage_check",
            conditions={"plan_type": "HMO"},
            effective_date=date(2025, 1, 1),
        )
        assert rule.agent_type == "eligibility"
        assert rule.conditions == {"plan_type": "HMO"}
        assert rule.termination_date is None
        assert rule.version == 1
        assert rule.actions is None
        assert rule.description is None

    def test_full_valid(self):
        rule = PayerRuleCreate(
            agent_type="claims",
            rule_type="code_validation",
            description="Require modifier 25 for E/M with procedure",
            conditions={"has_procedure": True, "code_type": "CPT"},
            actions={"add_modifier": "25"},
            effective_date=date(2025, 1, 1),
            termination_date=date(2025, 12, 31),
            version=2,
        )
        assert rule.actions == {"add_modifier": "25"}
        assert rule.version == 2

    def test_missing_conditions_raises(self):
        with pytest.raises(ValidationError):
            PayerRuleCreate(
                agent_type="eligibility",
                rule_type="coverage_check",
                effective_date=date(2025, 1, 1),
                # missing conditions
            )


class TestPayerRuleResponse:
    def test_full_response(self):
        rid = uuid.uuid4()
        pid = uuid.uuid4()
        now = datetime.now(timezone.utc)
        resp = PayerRuleResponse(
            id=rid,
            payer_id=pid,
            agent_type="prior_auth",
            rule_type="pa_required",
            conditions={"procedure_code": "27447"},
            effective_date=date(2025, 1, 1),
            created_at=now,
        )
        assert resp.payer_id == pid
        assert resp.is_active is True
        assert resp.version == 1

    def test_from_attributes_config(self):
        assert PayerRuleResponse.model_config.get("from_attributes") is True


class TestPayerRuleUpdate:
    def test_all_optional(self):
        update = PayerRuleUpdate()
        assert update.conditions is None
        assert update.actions is None
        assert update.description is None
        assert update.termination_date is None
        assert update.is_active is None

    def test_partial_update(self):
        update = PayerRuleUpdate(
            conditions={"new_condition": True},
            is_active=False,
        )
        assert update.conditions == {"new_condition": True}
        assert update.is_active is False


class TestPayerRuleEvaluationRequest:
    def test_valid(self):
        req = PayerRuleEvaluationRequest(
            context={"procedure_code": "27447", "payer_id": "AET01"},
        )
        assert req.context["procedure_code"] == "27447"
        assert req.rule_type is None

    def test_with_rule_type(self):
        req = PayerRuleEvaluationRequest(
            context={"procedure_code": "27447"},
            rule_type="pa_required",
        )
        assert req.rule_type == "pa_required"

    def test_missing_context_raises(self):
        with pytest.raises(ValidationError):
            PayerRuleEvaluationRequest()
