"""Unit tests for HITL escalation and review queue.

Covers: confidence threshold evaluation, automatic review creation,
review lifecycle (create → assign → approve/reject/escalate), and
audit trail logging.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.core.hitl.escalation import (
    EscalationConfig,
    EscalationManager,
)
from app.core.hitl.review_queue import (
    ReviewQueue,
    ReviewNotFoundError,
    ReviewStateError,
)
from app.models.agent_task import AgentTask
from app.models.audit import AuditLog
from app.models.hitl_review import HITLReview
from app.models.organization import Organization

from sqlalchemy import select


# ── Escalation Config Tests ─────────────────────────────────────────


class TestEscalationConfig:
    def test_default_threshold(self):
        config = EscalationConfig()
        assert config.get_threshold("eligibility") == 0.7

    def test_custom_agent_threshold(self):
        config = EscalationConfig(
            default_threshold=0.7,
            agent_thresholds={"claims": 0.8, "prior_auth": 0.6},
        )
        assert config.get_threshold("claims") == 0.8
        assert config.get_threshold("prior_auth") == 0.6
        assert config.get_threshold("eligibility") == 0.7  # uses default


# ── Escalation Manager Tests ───────────────────────────────────────


class TestEscalationManager:
    async def _create_task(self, session, agent_type="eligibility"):
        """Create a test agent task."""
        org = Organization(name="Test Org")
        session.add(org)
        await session.flush()

        task = AgentTask(
            agent_type=agent_type,
            status="running",
            organization_id=org.id,
            input_data={"test": True},
        )
        session.add(task)
        await session.flush()
        return task

    def test_should_escalate_low_confidence(self):
        manager = EscalationManager.__new__(EscalationManager)
        manager._config = EscalationConfig(default_threshold=0.7)
        should, reason = manager.should_escalate(
            confidence=0.4, agent_type="eligibility"
        )
        assert should is True
        assert "0.40" in reason
        assert "0.70" in reason

    def test_should_not_escalate_high_confidence(self):
        manager = EscalationManager.__new__(EscalationManager)
        manager._config = EscalationConfig(default_threshold=0.7)
        should, reason = manager.should_escalate(
            confidence=0.9, agent_type="eligibility"
        )
        assert should is False
        assert reason == ""

    def test_should_escalate_on_error(self):
        manager = EscalationManager.__new__(EscalationManager)
        manager._config = EscalationConfig(auto_escalate_errors=True)
        should, reason = manager.should_escalate(
            confidence=0.9, agent_type="claims", has_error=True
        )
        assert should is True
        assert "error" in reason.lower()

    def test_should_not_escalate_error_when_disabled(self):
        manager = EscalationManager.__new__(EscalationManager)
        manager._config = EscalationConfig(auto_escalate_errors=False)
        should, reason = manager.should_escalate(
            confidence=0.9, agent_type="claims", has_error=True
        )
        assert should is False

    async def test_evaluate_and_escalate_creates_review(self, db_session):
        task = await self._create_task(db_session)
        config = EscalationConfig(default_threshold=0.7)
        manager = EscalationManager(db_session, config)

        review = await manager.evaluate_and_escalate(
            task_id=str(task.id),
            agent_type="eligibility",
            confidence=0.4,
            agent_decision={"coverage": "ambiguous"},
        )

        assert review is not None
        assert review.status == "pending"
        assert review.confidence_score == 0.4
        assert "0.40" in review.reason

        # Verify task status was updated to 'review'
        await db_session.refresh(task)
        assert task.status == "review"

    async def test_evaluate_and_escalate_no_review_when_confident(self, db_session):
        task = await self._create_task(db_session)
        config = EscalationConfig(default_threshold=0.7)
        manager = EscalationManager(db_session, config)

        review = await manager.evaluate_and_escalate(
            task_id=str(task.id),
            agent_type="eligibility",
            confidence=0.9,
        )

        assert review is None
        # Task status should NOT have changed
        await db_session.refresh(task)
        assert task.status == "running"

    async def test_escalation_creates_audit_log(self, db_session):
        task = await self._create_task(db_session)
        manager = EscalationManager(db_session)

        review = await manager.evaluate_and_escalate(
            task_id=str(task.id),
            agent_type="eligibility",
            confidence=0.3,
        )

        # Check audit log entries (order by created_at for deterministic results)
        stmt = select(AuditLog).where(
            AuditLog.action == "hitl_escalation_created"
        ).order_by(AuditLog.timestamp.desc())
        result = await db_session.execute(stmt)
        entries = list(result.scalars().all())
        assert len(entries) >= 1
        # Verify the most recent entry matches this review
        matching = [e for e in entries if e.resource_id == str(review.id)]
        assert len(matching) >= 1

    async def test_evaluate_state(self, db_session):
        task = await self._create_task(db_session)
        manager = EscalationManager(db_session)

        from app.core.engine.state import create_initial_state

        state = create_initial_state(
            task_id=str(task.id),
            agent_type="eligibility",
        )
        state["confidence"] = 0.3

        review = await manager.evaluate_state(state)
        assert review is not None
        assert review.status == "pending"


# ── Review Queue Tests ──────────────────────────────────────────────


class TestReviewQueue:
    async def _create_task_and_review(
        self, session, agent_type="eligibility", confidence=0.5
    ):
        """Create a task and a pending review for it."""
        org = Organization(name="Test Org")
        session.add(org)
        await session.flush()

        task = AgentTask(
            agent_type=agent_type,
            status="review",
            organization_id=org.id,
        )
        session.add(task)
        await session.flush()

        review = HITLReview(
            task_id=task.id,
            status="pending",
            reason="Low confidence",
            confidence_score=confidence,
            agent_decision={"result": "uncertain"},
        )
        session.add(review)
        await session.flush()
        return task, review

    async def test_create_review(self, db_session):
        """ReviewQueue.create() creates a review and audit-logs it."""
        org = Organization(name="Test Org")
        db_session.add(org)
        await db_session.flush()

        task = AgentTask(
            agent_type="eligibility",
            status="running",
            organization_id=org.id,
        )
        db_session.add(task)
        await db_session.flush()

        queue = ReviewQueue(db_session)
        review = await queue.create(
            task_id=str(task.id),
            reason="Low confidence test",
            agent_decision={"result": "uncertain"},
            confidence_score=0.45,
        )

        assert review is not None
        assert review.status == "pending"
        assert review.reason == "Low confidence test"
        assert review.confidence_score == 0.45
        assert review.agent_decision == {"result": "uncertain"}
        assert str(review.task_id) == str(task.id)

        # Verify audit log entry was created (use set-based match for determinism)
        stmt = select(AuditLog).where(
            AuditLog.action == "hitl_review_created"
        ).order_by(AuditLog.timestamp.desc())
        result = await db_session.execute(stmt)
        entries = list(result.scalars().all())
        assert len(entries) >= 1
        matching = [e for e in entries if e.resource_id == str(review.id)]
        assert len(matching) >= 1

    async def test_create_assign_approve_lifecycle(self, db_session):
        """Full lifecycle via ReviewQueue: create → assign → approve → audit."""
        org = Organization(name="Test Org")
        db_session.add(org)
        await db_session.flush()

        task = AgentTask(
            agent_type="claims",
            status="running",
            organization_id=org.id,
        )
        db_session.add(task)
        await db_session.flush()

        queue = ReviewQueue(db_session)

        # 1. Create
        review = await queue.create(
            task_id=str(task.id),
            reason="Code confidence low",
            confidence_score=0.4,
        )
        assert review.status == "pending"

        # 2. Assign
        reviewer_id = str(uuid.uuid4())
        assigned = await queue.assign_reviewer(str(review.id), reviewer_id)
        assert str(assigned.reviewer_id) == reviewer_id

        # 3. Approve
        approved = await queue.approve(
            str(review.id),
            reviewer_id=reviewer_id,
            notes="Codes verified correct",
        )
        assert approved.status == "approved"
        assert approved.decided_at is not None

        # 4. Verify task was updated
        await db_session.refresh(task)
        assert task.status == "completed"

        # 5. Verify full audit trail
        stmt = select(AuditLog).where(
            AuditLog.resource_id == str(review.id)
        )
        result = await db_session.execute(stmt)
        entries = list(result.scalars().all())
        actions = {e.action for e in entries}
        assert "hitl_review_created" in actions
        assert "hitl_reviewer_assigned" in actions
        assert "hitl_review_approved" in actions

    async def test_list_pending_reviews(self, db_session):
        _, review = await self._create_task_and_review(db_session)
        queue = ReviewQueue(db_session)
        reviews = await queue.list_reviews(status="pending")
        assert len(reviews) >= 1
        assert any(r.id == review.id for r in reviews)

    async def test_list_reviews_by_agent_type(self, db_session):
        await self._create_task_and_review(db_session, agent_type="eligibility")
        await self._create_task_and_review(db_session, agent_type="claims")

        queue = ReviewQueue(db_session)
        elig_reviews = await queue.list_reviews(agent_type="eligibility")
        assert len(elig_reviews) >= 1
        # Verify every returned review is linked to an eligibility task
        for review in elig_reviews:
            task_stmt = select(AgentTask).where(AgentTask.id == review.task_id)
            result = await db_session.execute(task_stmt)
            task = result.scalar_one()
            assert task.agent_type == "eligibility"

        # Also verify claims reviews are separate
        claims_reviews = await queue.list_reviews(agent_type="claims")
        assert len(claims_reviews) >= 1
        for review in claims_reviews:
            task_stmt = select(AgentTask).where(AgentTask.id == review.task_id)
            result = await db_session.execute(task_stmt)
            task = result.scalar_one()
            assert task.agent_type == "claims"

    async def test_get_review(self, db_session):
        _, review = await self._create_task_and_review(db_session)
        queue = ReviewQueue(db_session)
        fetched = await queue.get_review(str(review.id))
        assert fetched.id == review.id

    async def test_get_review_not_found(self, db_session):
        queue = ReviewQueue(db_session)
        with pytest.raises(ReviewNotFoundError):
            await queue.get_review(str(uuid.uuid4()))

    async def test_get_pending_count(self, db_session):
        await self._create_task_and_review(db_session)
        queue = ReviewQueue(db_session)
        count = await queue.get_pending_count()
        assert count >= 1

    async def test_assign_reviewer(self, db_session):
        _, review = await self._create_task_and_review(db_session)
        reviewer_id = str(uuid.uuid4())
        queue = ReviewQueue(db_session)
        updated = await queue.assign_reviewer(str(review.id), reviewer_id)
        assert str(updated.reviewer_id) == reviewer_id

    async def test_assign_reviewer_wrong_status(self, db_session):
        _, review = await self._create_task_and_review(db_session)
        review.status = "approved"
        await db_session.flush()

        queue = ReviewQueue(db_session)
        with pytest.raises(ReviewStateError, match="Cannot assign"):
            await queue.assign_reviewer(str(review.id), str(uuid.uuid4()))

    async def test_approve_review(self, db_session):
        task, review = await self._create_task_and_review(db_session)
        reviewer_id = str(uuid.uuid4())
        queue = ReviewQueue(db_session)

        approved = await queue.approve(
            str(review.id),
            reviewer_id=reviewer_id,
            notes="Looks good",
        )

        assert approved.status == "approved"
        assert approved.reviewer_notes == "Looks good"
        assert approved.decided_at is not None

        # Task should be completed
        await db_session.refresh(task)
        assert task.status == "completed"

    async def test_approve_creates_audit_log(self, db_session):
        _, review = await self._create_task_and_review(db_session)
        reviewer_id = str(uuid.uuid4())
        queue = ReviewQueue(db_session)

        await queue.approve(str(review.id), reviewer_id=reviewer_id)

        stmt = select(AuditLog).where(
            AuditLog.action == "hitl_review_approved"
        )
        result = await db_session.execute(stmt)
        entries = list(result.scalars().all())
        assert len(entries) >= 1

    async def test_reject_review(self, db_session):
        task, review = await self._create_task_and_review(db_session)
        reviewer_id = str(uuid.uuid4())
        queue = ReviewQueue(db_session)

        rejected = await queue.reject(
            str(review.id),
            reviewer_id=reviewer_id,
            notes="Incorrect coding",
        )

        assert rejected.status == "rejected"
        assert rejected.reviewer_notes == "Incorrect coding"
        assert rejected.decided_at is not None

        # Task should be failed
        await db_session.refresh(task)
        assert task.status == "failed"

    async def test_reject_creates_audit_log(self, db_session):
        _, review = await self._create_task_and_review(db_session)
        queue = ReviewQueue(db_session)

        await queue.reject(str(review.id), reviewer_id=str(uuid.uuid4()))

        stmt = select(AuditLog).where(
            AuditLog.action == "hitl_review_rejected"
        )
        result = await db_session.execute(stmt)
        entries = list(result.scalars().all())
        assert len(entries) >= 1

    async def test_escalate_review(self, db_session):
        _, review = await self._create_task_and_review(db_session)
        reviewer_id = str(uuid.uuid4())
        queue = ReviewQueue(db_session)

        escalated = await queue.escalate(
            str(review.id),
            reviewer_id=reviewer_id,
            notes="Need supervisor opinion",
        )

        assert escalated.status == "escalated"

    async def test_escalate_creates_audit_log(self, db_session):
        _, review = await self._create_task_and_review(db_session)
        queue = ReviewQueue(db_session)

        await queue.escalate(str(review.id), reviewer_id=str(uuid.uuid4()))

        stmt = select(AuditLog).where(
            AuditLog.action == "hitl_review_escalated"
        )
        result = await db_session.execute(stmt)
        entries = list(result.scalars().all())
        assert len(entries) >= 1

    async def test_approve_escalated_review(self, db_session):
        """Escalated reviews can be approved by a supervisor."""
        task, review = await self._create_task_and_review(db_session)
        queue = ReviewQueue(db_session)

        # First escalate
        await queue.escalate(str(review.id), reviewer_id=str(uuid.uuid4()))

        # Then approve the escalated review
        supervisor_id = str(uuid.uuid4())
        approved = await queue.approve(
            str(review.id),
            reviewer_id=supervisor_id,
            notes="Supervisor approved",
        )
        assert approved.status == "approved"

        await db_session.refresh(task)
        assert task.status == "completed"

    async def test_cannot_approve_already_approved(self, db_session):
        _, review = await self._create_task_and_review(db_session)
        queue = ReviewQueue(db_session)

        await queue.approve(str(review.id), reviewer_id=str(uuid.uuid4()))

        with pytest.raises(ReviewStateError, match="Cannot approve"):
            await queue.approve(str(review.id), reviewer_id=str(uuid.uuid4()))

    async def test_full_lifecycle_create_assign_approve(self, db_session):
        """Full lifecycle: create → assign → approve → verify audit."""
        task, review = await self._create_task_and_review(db_session)
        reviewer_id = str(uuid.uuid4())
        queue = ReviewQueue(db_session)

        # 1. Assign
        await queue.assign_reviewer(str(review.id), reviewer_id)
        assert (await queue.get_review(str(review.id))).reviewer_id is not None

        # 2. Approve
        approved = await queue.approve(
            str(review.id),
            reviewer_id=reviewer_id,
            notes="Approved after review",
        )
        assert approved.status == "approved"

        # 3. Verify task completed
        await db_session.refresh(task)
        assert task.status == "completed"

        # 4. Verify audit trail
        stmt = select(AuditLog).where(
            AuditLog.resource_id == str(review.id)
        )
        result = await db_session.execute(stmt)
        entries = list(result.scalars().all())
        actions = {e.action for e in entries}
        assert "hitl_reviewer_assigned" in actions
        assert "hitl_review_approved" in actions
