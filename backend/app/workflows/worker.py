"""Temporal worker bootstrap — registers workflows and activities.

The worker process connects to the Temporal server and polls for tasks.
It registers all agent workflow types and their associated activities,
then begins processing tasks from the configured task queue.

Usage:
    python -m app.workflows.worker

Or via the CLI entrypoint defined in pyproject.toml.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any

from temporalio.client import Client
from temporalio.worker import Worker

from app.config import settings
from app.workflows.base import DEFAULT_TASK_QUEUE

logger = logging.getLogger(__name__)

# Registry of workflows and activities for Temporal worker registration
_WORKFLOW_REGISTRY: list[type] = []
_ACTIVITY_REGISTRY: list[Any] = []


def register_workflow(cls: type) -> type:
    """Register a workflow class for Temporal worker registration."""
    if cls not in _WORKFLOW_REGISTRY:
        _WORKFLOW_REGISTRY.append(cls)
    return cls


def register_activity(fn: Any) -> Any:
    """Register an activity function for Temporal worker registration."""
    if fn not in _ACTIVITY_REGISTRY:
        _ACTIVITY_REGISTRY.append(fn)
    return fn


def get_registered_workflows() -> list[type]:
    """Return all registered workflow classes."""
    return list(_WORKFLOW_REGISTRY)


def get_registered_activities() -> list[Any]:
    """Return all registered activity functions."""
    return list(_ACTIVITY_REGISTRY)


# ── Register eligibility workflow and activities ─────────────────────

from app.workflows.eligibility import (  # noqa: E402
    EligibilityWorkflow,
    validate_eligibility_input,
    create_pending_eligibility_check,
    execute_eligibility_agent,
    build_eligibility_request,
    submit_to_clearinghouse,
    parse_eligibility_response,
    write_eligibility_result,
)

register_workflow(EligibilityWorkflow)
register_activity(validate_eligibility_input)
register_activity(create_pending_eligibility_check)
register_activity(execute_eligibility_agent)
register_activity(build_eligibility_request)
register_activity(submit_to_clearinghouse)
register_activity(parse_eligibility_response)
register_activity(write_eligibility_result)

# ── Register generic agent workflow and activities ────────────────────

from app.workflows.agent_workflow import (  # noqa: E402
    GenericAgentWorkflow,
    validate_agent_input,
    execute_agent,
    write_agent_result,
)

register_workflow(GenericAgentWorkflow)
register_activity(validate_agent_input)
register_activity(execute_agent)
register_activity(write_agent_result)


# ── Register scheduling workflow and activities ─────────────────────

from app.workflows.scheduling import (  # noqa: E402
    SchedulingWorkflow,
    validate_scheduling_input,
    run_scheduling_agent_activity,
    write_scheduling_result,
)

register_workflow(SchedulingWorkflow)
register_activity(validate_scheduling_input)
register_activity(run_scheduling_agent_activity)
register_activity(write_scheduling_result)


# ── Register claims workflow and activities ──────────────────────────

from app.workflows.claims import (  # noqa: E402
    ClaimsWorkflow,
    validate_claims_input,
    run_claims_agent_activity,
    submit_claim_to_clearinghouse,
    parse_remittance_activity,
    write_claims_result,
    poll_claim_status,
    update_claim_status_activity,
    analyze_workflow_denials,
)

register_workflow(ClaimsWorkflow)
register_activity(validate_claims_input)
register_activity(run_claims_agent_activity)
register_activity(submit_claim_to_clearinghouse)
register_activity(parse_remittance_activity)
register_activity(write_claims_result)
register_activity(poll_claim_status)
register_activity(update_claim_status_activity)
register_activity(analyze_workflow_denials)


# ── Register prior auth workflow and activities ──────────────────────

from app.workflows.prior_auth import (  # noqa: E402
    PriorAuthWorkflow,
    validate_prior_auth_input,
    create_pending_pa_record,
    execute_prior_auth_agent,
    write_prior_auth_result,
    poll_pa_status_activity,
    generate_post_poll_appeal,
)

register_workflow(PriorAuthWorkflow)
register_activity(validate_prior_auth_input)
register_activity(create_pending_pa_record)
register_activity(execute_prior_auth_agent)
register_activity(write_prior_auth_result)
register_activity(poll_pa_status_activity)
register_activity(generate_post_poll_appeal)


# ── Register credentialing workflow and activities ──────────────────────

from app.workflows.credentialing import (  # noqa: E402
    CredentialingWorkflow,
    validate_credentialing_input,
    run_credentialing_agent_activity,
    write_credentialing_result,
    check_credentialing_status_activity,
    alert_expiration_activity,
)

register_workflow(CredentialingWorkflow)
register_activity(validate_credentialing_input)
register_activity(run_credentialing_agent_activity)
register_activity(write_credentialing_result)
register_activity(check_credentialing_status_activity)
register_activity(alert_expiration_activity)


# ── Register compliance workflow and activities ─────────────────────────

from app.workflows.compliance import (  # noqa: E402
    ComplianceWorkflow,
    validate_compliance_input,
    run_compliance_agent_activity,
    write_compliance_result,
)

register_workflow(ComplianceWorkflow)
register_activity(validate_compliance_input)
register_activity(run_compliance_agent_activity)
register_activity(write_compliance_result)


# ── Worker Creation ─────────────────────────────────────────────────────


async def create_worker(
    *,
    temporal_address: str | None = None,
    task_queue: str | None = None,
    namespace: str = "default",
    client: Client | None = None,
) -> Worker:
    """Create and configure a Temporal worker.

    Connects to the Temporal server (or reuses *client*) and returns a
    ``Worker`` instance ready to be started with ``await worker.run()``.

    Args:
        temporal_address: Temporal server address (host:port).
        task_queue: Task queue name to poll.
        namespace: Temporal namespace.
        client: Optional pre-connected ``Client`` to reuse.

    Returns:
        A ``temporalio.worker.Worker`` instance.
    """
    address = temporal_address or settings.temporal_address
    queue = task_queue or settings.temporal_task_queue

    if client is None:
        client = await Client.connect(address, namespace=namespace)

    workflows = get_registered_workflows()
    activities = get_registered_activities()

    worker = Worker(
        client,
        task_queue=queue,
        workflows=workflows,
        activities=activities,
    )

    logger.info(
        "Temporal worker created: address=%s, queue=%s, "
        "workflows=%d, activities=%d",
        address, queue, len(workflows), len(activities),
    )

    return worker


async def run_worker(
    *,
    temporal_address: str | None = None,
    task_queue: str | None = None,
    namespace: str = "default",
) -> None:
    """Start the Temporal worker and begin polling for tasks.

    This is the main entry point for the worker process.  It creates a
    Temporal client, builds the worker, installs shutdown signal handlers,
    and runs until interrupted.
    """
    address = temporal_address or settings.temporal_address
    queue = task_queue or settings.temporal_task_queue

    logger.info(
        "Connecting to Temporal at %s (namespace=%s, queue=%s) ...",
        address, namespace, queue,
    )

    client = await Client.connect(address, namespace=namespace)
    worker = await create_worker(
        task_queue=queue,
        client=client,
    )

    logger.info("Temporal worker started: polling %s", queue)

    # Graceful shutdown on SIGTERM / SIGINT
    shutdown_event = asyncio.Event()

    def _handle_signal() -> None:
        logger.info("Shutdown signal received, stopping worker...")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass  # Windows doesn't support add_signal_handler

    # Run the worker until shutdown is requested
    async with worker:
        await shutdown_event.wait()

    logger.info("Temporal worker stopped.")


# ── CLI Entrypoint ──────────────────────────────────────────────────────


def main() -> None:
    """CLI entrypoint for running the Temporal worker."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger.info("Starting Slate Health Temporal worker...")

    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        logger.info("Worker interrupted.")
        sys.exit(0)


if __name__ == "__main__":
    main()
