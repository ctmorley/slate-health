"""Base Temporal workflow and activity classes with retry policies and error handling.

Provides reusable patterns for wrapping agent execution in durable Temporal
workflows. Activities handle the actual work (agent invocation, clearinghouse
calls, DB writes), while workflows orchestrate the activity sequence.

Includes abstract base classes ``BaseWorkflow`` and ``BaseActivity`` that
concrete implementations inherit from, ensuring a consistent interface for
all agent workflows and activities.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from typing import Any

from temporalio.common import RetryPolicy

from app.core.logging_config import _correlation_id_ctx

logger = logging.getLogger(__name__)


def set_workflow_correlation_id(correlation_id: str) -> None:
    """Set the correlation ID context variable from a workflow/activity.

    Activities should call this at the start of execution with the
    ``correlation_id`` from :class:`WorkflowInput` so that all subsequent
    log records emitted during the activity include the correlation ID
    that originated from the HTTP request.
    """
    _correlation_id_ctx.set(correlation_id)


def restore_correlation_id(activity_input: dict) -> None:
    """Restore the correlation ID from an activity input dict.

    Call this at the top of every ``@activity.defn`` function to propagate
    the correlation ID from the workflow into the activity's log context.
    Safe to call even if ``correlation_id`` is missing from the dict.
    """
    cid = ""
    if isinstance(activity_input, dict):
        cid = activity_input.get("correlation_id", "")
    if cid:
        _correlation_id_ctx.set(cid)


# ── Retry Policies ──────────────────────────────────────────────────────


class RetryPolicyConfig:
    """Configuration for activity retry behavior.

    Maps to Temporal RetryPolicy parameters. Used by activity decorators
    to configure retry behavior.
    """

    def __init__(
        self,
        *,
        initial_interval_seconds: float = 1.0,
        backoff_coefficient: float = 2.0,
        maximum_interval_seconds: float = 60.0,
        maximum_attempts: int = 3,
        non_retryable_error_types: list[str] | None = None,
    ):
        self.initial_interval_seconds = initial_interval_seconds
        self.backoff_coefficient = backoff_coefficient
        self.maximum_interval_seconds = maximum_interval_seconds
        self.maximum_attempts = maximum_attempts
        self.non_retryable_error_types = non_retryable_error_types or []

    def to_temporal_dict(self) -> dict[str, Any]:
        """Convert to a dict compatible with temporalio.common.RetryPolicy kwargs."""
        return {
            "initial_interval": timedelta(seconds=self.initial_interval_seconds),
            "backoff_coefficient": self.backoff_coefficient,
            "maximum_interval": timedelta(seconds=self.maximum_interval_seconds),
            "maximum_attempts": self.maximum_attempts,
            "non_retryable_error_types": self.non_retryable_error_types,
        }

    def to_retry_policy(self) -> RetryPolicy:
        """Convert to a real temporalio.common.RetryPolicy instance."""
        return RetryPolicy(
            initial_interval=timedelta(seconds=self.initial_interval_seconds),
            backoff_coefficient=self.backoff_coefficient,
            maximum_interval=timedelta(seconds=self.maximum_interval_seconds),
            maximum_attempts=self.maximum_attempts,
            non_retryable_error_types=self.non_retryable_error_types,
        )


# Common retry policies for different activity types
AGENT_RETRY_POLICY = RetryPolicyConfig(
    initial_interval_seconds=2.0,
    backoff_coefficient=2.0,
    maximum_interval_seconds=120.0,
    maximum_attempts=3,
    non_retryable_error_types=["ValidationError", "ClearinghouseValidationError"],
)

CLEARINGHOUSE_RETRY_POLICY = RetryPolicyConfig(
    initial_interval_seconds=5.0,
    backoff_coefficient=2.0,
    maximum_interval_seconds=300.0,
    maximum_attempts=5,
    non_retryable_error_types=["ClearinghouseValidationError"],
)

DB_RETRY_POLICY = RetryPolicyConfig(
    initial_interval_seconds=1.0,
    backoff_coefficient=2.0,
    maximum_interval_seconds=30.0,
    maximum_attempts=3,
)


# ── Workflow Status ─────────────────────────────────────────────────────


class WorkflowStatus(str, Enum):
    """Status values for workflow executions."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


# ── Data Transfer Objects ───────────────────────────────────────────────


@dataclass
class WorkflowInput:
    """Common input for all agent workflows.

    This is the serializable input passed to Temporal workflows.
    """

    task_id: str
    agent_type: str
    input_data: dict[str, Any] = field(default_factory=dict)
    patient_context: dict[str, Any] = field(default_factory=dict)
    payer_context: dict[str, Any] = field(default_factory=dict)
    organization_id: str | None = None
    clearinghouse_config: dict[str, Any] | None = None
    correlation_id: str = ""


@dataclass
class WorkflowResult:
    """Common result from all agent workflows."""

    task_id: str
    agent_type: str
    status: str = "completed"
    output_data: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    needs_review: bool = False
    review_reason: str = ""
    error: str | None = None
    clearinghouse_transaction_id: str | None = None


@dataclass
class ActivityResult:
    """Result from an individual activity execution."""

    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


# ── Activity Heartbeat Mixin ────────────────────────────────────────────


class HeartbeatMixin:
    """Mixin for activities that need periodic heartbeat reporting.

    Temporal uses heartbeats to detect activity liveness. Long-running
    activities should call heartbeat() periodically to prevent timeout.
    """

    _heartbeat_fn: Any = None

    def set_heartbeat_fn(self, fn: Any) -> None:
        """Set the heartbeat function (typically activity.heartbeat)."""
        self._heartbeat_fn = fn

    async def heartbeat(self, details: Any = None) -> None:
        """Send a heartbeat to Temporal, if a heartbeat function is set."""
        if self._heartbeat_fn is not None:
            try:
                self._heartbeat_fn(details)
            except Exception:
                logger.debug("Heartbeat call failed (non-fatal)")


def safe_heartbeat(details: Any = None) -> None:
    """Send a heartbeat if running inside a Temporal activity context.

    Safe to call from both Temporal and non-Temporal (inline/test)
    execution — silently no-ops when no activity context is present.
    """
    from temporalio import activity as _act
    try:
        _act.heartbeat(details)
    except RuntimeError:
        # Not inside a Temporal activity context — no-op
        pass


# ── Workflow Error Types ────────────────────────────────────────────────


class WorkflowError(Exception):
    """Base error for workflow execution failures."""

    def __init__(self, message: str, task_id: str = "", details: dict[str, Any] | None = None):
        super().__init__(message)
        self.task_id = task_id
        self.details = details or {}


class ActivityError(WorkflowError):
    """Error during activity execution."""
    pass


class WorkflowTimeoutError(WorkflowError):
    """Workflow exceeded its time limit."""
    pass


class WorkflowCancelledError(WorkflowError):
    """Workflow was cancelled by user or system."""
    pass


# ── Default Timeouts ────────────────────────────────────────────────────

# Activity execution timeouts
AGENT_ACTIVITY_TIMEOUT = timedelta(minutes=5)
CLEARINGHOUSE_ACTIVITY_TIMEOUT = timedelta(minutes=2)
DB_ACTIVITY_TIMEOUT = timedelta(seconds=30)

# Heartbeat timeouts — activities must heartbeat within this interval
# or Temporal considers them stalled and reschedules.
CLEARINGHOUSE_HEARTBEAT_TIMEOUT = timedelta(seconds=60)
DB_HEARTBEAT_TIMEOUT = timedelta(seconds=15)

# Workflow-level timeouts
DEFAULT_WORKFLOW_EXECUTION_TIMEOUT = timedelta(hours=1)
LONG_RUNNING_WORKFLOW_TIMEOUT = timedelta(days=90)  # For credentialing

# Task queue names
DEFAULT_TASK_QUEUE = "slate-health-agents"
CLEARINGHOUSE_TASK_QUEUE = "slate-health-clearinghouse"


# ── Abstract Base Classes ─────────────────────────────────────────────


class BaseWorkflow(abc.ABC):
    """Abstract base class for all Temporal workflow implementations.

    Concrete workflows should inherit from this class and implement
    the ``run`` method, which is decorated with ``@workflow.run`` in
    the subclass.  The base class provides shared helpers and enforces
    a consistent interface across all agent workflows.
    """

    @abc.abstractmethod
    async def run(self, workflow_input: WorkflowInput) -> WorkflowResult:
        """Execute the workflow.

        Subclasses must implement this method and decorate it with
        ``@workflow.run``.  It receives a :class:`WorkflowInput` and
        must return a :class:`WorkflowResult`.
        """
        ...

    @staticmethod
    def _fail(task_id: str, agent_type: str, error: str) -> WorkflowResult:
        """Convenience helper to build a failed :class:`WorkflowResult`."""
        return WorkflowResult(
            task_id=task_id,
            agent_type=agent_type,
            status=WorkflowStatus.FAILED.value,
            error=error,
        )


class BaseActivity(abc.ABC):
    """Abstract base class for stateful Temporal activities.

    Activities that need to maintain state (e.g. a DB session factory,
    an HTTP client, or a heartbeat handle) should inherit from this
    class.  Stateless activities can remain plain ``@activity.defn``
    functions, but complex activities benefit from the structure and
    testability this base provides.

    Subclasses must implement :meth:`execute`.  The subclass should
    also be decorated with ``@activity.defn`` and expose ``execute``
    as the activity entrypoint.
    """

    def __init__(self) -> None:
        self._heartbeat_fn: Any = None

    def set_heartbeat_fn(self, fn: Any) -> None:
        """Inject a heartbeat function (typically ``activity.heartbeat``)."""
        self._heartbeat_fn = fn

    async def heartbeat(self, details: Any = None) -> None:
        """Send a heartbeat if a function was injected; no-op otherwise."""
        if self._heartbeat_fn is not None:
            try:
                self._heartbeat_fn(details)
            except Exception:
                logger.debug("Heartbeat call failed (non-fatal)")

    @abc.abstractmethod
    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        """Implement the activity logic.

        Subclasses must override this method.  It will be invoked by
        Temporal when the activity is scheduled.
        """
        ...
