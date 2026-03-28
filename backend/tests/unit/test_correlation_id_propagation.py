"""Unit tests for correlation ID propagation across API → workflow → activity.

Verifies that:
- CorrelationIdMiddleware generates and attaches IDs to responses
- Correlation ID flows through WorkflowInput into activity dicts
- restore_correlation_id() correctly sets the context variable
- Activities that call restore_correlation_id() emit logs with the ID
"""

from __future__ import annotations

import uuid

import pytest

from app.core.logging_config import _correlation_id_ctx, get_correlation_id
from app.workflows.base import (
    WorkflowInput,
    restore_correlation_id,
    set_workflow_correlation_id,
)


class TestSetWorkflowCorrelationId:
    """Test set_workflow_correlation_id context management."""

    def test_sets_correlation_id_in_context(self):
        cid = str(uuid.uuid4())
        set_workflow_correlation_id(cid)
        assert get_correlation_id() == cid

    def test_empty_string_clears_context(self):
        set_workflow_correlation_id("some-id")
        set_workflow_correlation_id("")
        assert get_correlation_id() == ""


class TestRestoreCorrelationId:
    """Test restore_correlation_id helper for activities."""

    def test_restores_from_dict(self):
        cid = f"test-{uuid.uuid4()}"
        restore_correlation_id({"correlation_id": cid, "task_id": "t1"})
        assert get_correlation_id() == cid

    def test_no_op_when_missing(self):
        # Set a known value first
        set_workflow_correlation_id("before")
        # Call with dict that has no correlation_id
        restore_correlation_id({"task_id": "t1"})
        # Should NOT clear the existing value (empty string is falsy → no-op)
        assert get_correlation_id() == "before"

    def test_no_op_when_not_dict(self):
        set_workflow_correlation_id("before")
        restore_correlation_id("not-a-dict")  # type: ignore
        assert get_correlation_id() == "before"

    def test_no_op_when_none(self):
        set_workflow_correlation_id("before")
        restore_correlation_id(None)  # type: ignore
        assert get_correlation_id() == "before"

    def test_restores_empty_string_is_no_op(self):
        set_workflow_correlation_id("original")
        restore_correlation_id({"correlation_id": ""})
        # Empty string should not overwrite
        assert get_correlation_id() == "original"


class TestWorkflowInputCarriesCorrelationId:
    """Test that WorkflowInput carries correlation_id to activities."""

    def test_default_is_empty_string(self):
        wi = WorkflowInput(task_id="t1", agent_type="eligibility")
        assert wi.correlation_id == ""

    def test_explicit_correlation_id(self):
        cid = str(uuid.uuid4())
        wi = WorkflowInput(
            task_id="t1",
            agent_type="eligibility",
            correlation_id=cid,
        )
        assert wi.correlation_id == cid


@pytest.mark.asyncio
async def test_correlation_id_in_api_response(client):
    """Health endpoint response should include X-Correlation-ID header."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert "x-correlation-id" in resp.headers
    cid = resp.headers["x-correlation-id"]
    # Should be a UUID4 or similar non-empty string
    assert len(cid) >= 8


@pytest.mark.asyncio
async def test_correlation_id_echoed_back(client):
    """When a request includes X-Correlation-ID, the same value is returned."""
    # Reset context var to avoid pollution from previous tests
    _correlation_id_ctx.set("")
    custom_id = f"test-echo-{uuid.uuid4()}"
    resp = await client.get(
        "/health",
        headers={"X-Correlation-ID": custom_id},
    )
    assert resp.status_code == 200
    assert resp.headers.get("x-correlation-id") == custom_id
