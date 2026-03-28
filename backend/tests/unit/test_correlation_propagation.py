"""Tests for correlation ID propagation from HTTP request through workflow/activity execution.

Verifies that:
1. set_workflow_correlation_id sets the context variable used by logging
2. Log records emitted after setting the correlation ID include it
3. The correlation ID from WorkflowInput can be propagated into activity context
"""

from __future__ import annotations

import json
import logging

import pytest

from app.core.logging_config import (
    CorrelationIdFilter,
    JSONFormatter,
    _correlation_id_ctx,
    get_correlation_id,
)
from app.workflows.base import WorkflowInput, set_workflow_correlation_id


class TestSetWorkflowCorrelationId:
    """Verify set_workflow_correlation_id sets the context for logging."""

    def test_sets_context_variable(self):
        token = _correlation_id_ctx.set("")
        try:
            set_workflow_correlation_id("wf-corr-abc-123")
            assert get_correlation_id() == "wf-corr-abc-123"
        finally:
            _correlation_id_ctx.reset(token)

    def test_empty_string_clears_context(self):
        token = _correlation_id_ctx.set("old-value")
        try:
            set_workflow_correlation_id("")
            assert get_correlation_id() == ""
        finally:
            _correlation_id_ctx.reset(token)


class TestCorrelationIdInLogRecords:
    """Verify that after setting the workflow correlation ID, log records include it."""

    def test_log_record_contains_correlation_id(self):
        token = _correlation_id_ctx.set("")
        try:
            correlation_id = "req-12345-abcde"
            set_workflow_correlation_id(correlation_id)

            # Create a logger with the correlation filter and JSON formatter
            test_logger = logging.getLogger("test.correlation.propagation")
            test_logger.setLevel(logging.DEBUG)
            test_logger.propagate = False

            handler = logging.StreamHandler()
            formatter = JSONFormatter()
            handler.setFormatter(formatter)
            filt = CorrelationIdFilter()
            handler.addFilter(filt)
            test_logger.addHandler(handler)

            try:
                # Create a log record through the filter
                record = logging.LogRecord(
                    name="test.correlation",
                    level=logging.INFO,
                    pathname="test.py",
                    lineno=1,
                    msg="Processing activity",
                    args=(),
                    exc_info=None,
                )
                filt.filter(record)

                assert record.correlation_id == correlation_id  # type: ignore[attr-defined]

                # Verify JSON output includes the correlation ID
                output = formatter.format(record)
                parsed = json.loads(output)
                assert parsed["correlation_id"] == correlation_id
            finally:
                test_logger.removeHandler(handler)
        finally:
            _correlation_id_ctx.reset(token)


class TestCorrelationPropagationFlow:
    """End-to-end test: correlation ID flows from WorkflowInput through to logging."""

    def test_workflow_input_correlation_id_propagates_to_logs(self):
        """Simulate an activity receiving WorkflowInput and setting correlation context."""
        token = _correlation_id_ctx.set("")
        try:
            # Simulate what the HTTP middleware does: set a correlation ID
            original_correlation_id = "http-req-uuid-999"

            # Simulate what workflow_service does: pack it into WorkflowInput
            wf_input = WorkflowInput(
                task_id="task-1",
                agent_type="eligibility",
                correlation_id=original_correlation_id,
            )

            # Simulate what an activity should do: rehydrate the correlation context
            set_workflow_correlation_id(wf_input.correlation_id)

            # Verify context is set
            assert get_correlation_id() == original_correlation_id

            # Verify log records pick it up
            filt = CorrelationIdFilter()
            record = logging.LogRecord(
                name="app.workflows.eligibility",
                level=logging.INFO,
                pathname="eligibility.py",
                lineno=50,
                msg="Calling clearinghouse API",
                args=(),
                exc_info=None,
            )
            filt.filter(record)
            assert record.correlation_id == original_correlation_id  # type: ignore[attr-defined]

            formatter = JSONFormatter()
            output = formatter.format(record)
            parsed = json.loads(output)
            assert parsed["correlation_id"] == original_correlation_id
            assert parsed["message"] == "Calling clearinghouse API"
        finally:
            _correlation_id_ctx.reset(token)
