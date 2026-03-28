"""Load test: concurrent eligibility task creation.

Spawns 50 concurrent requests to the eligibility agent endpoint and verifies
that all complete successfully with unique task IDs, no data corruption, no
5xx errors, and within the contracted 60-second time budget.

Contract requirement: all 50 tasks must reach ``status == "completed"`` —
``failed`` and ``review`` are treated as errors in this load test because
the contract states "no errors or data corruption".
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from app.core.auth.jwt import create_access_token

# ── Auth helper ───────────────────────────────────────────────────────

_TEST_USER_ID = uuid.uuid4()
_TEST_USER_EMAIL = "e2e-loadtest@slate.health"


def _auth_header(role: str = "admin") -> dict[str, str]:
    token = create_access_token(
        user_id=_TEST_USER_ID,
        email=_TEST_USER_EMAIL,
        role=role,
        full_name="E2E Load Tester",
    )
    return {"Authorization": f"Bearer {token}"}


# ── Load test constants ───────────────────────────────────────────────

CONCURRENT_REQUESTS = 50
# Contract: all 50 concurrent tasks must complete within 60 seconds.
MAX_TOTAL_SECONDS = 60

# Used only for polling termination — final assertions check for "completed"
TERMINAL_STATUSES = {"completed", "failed", "review"}


# ── Polling helper ────────────────────────────────────────────────────


async def _poll_until_terminal(client, agent_type: str, task_id: str, headers: dict,
                                timeout: float = 30.0, interval: float = 0.5) -> dict:
    """Poll task status until it reaches a terminal state."""
    status = None
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        resp = await client.get(
            f"/api/v1/agents/{agent_type}/tasks/{task_id}",
            headers=headers,
        )
        assert resp.status_code == 200
        status = resp.json()["status"]
        if status in TERMINAL_STATUSES:
            return resp.json()
        await asyncio.sleep(interval)
    raise AssertionError(
        f"Task {task_id} did not reach terminal status within {timeout}s (last status: {status})"
    )


# ── Load test ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_eligibility_task_creation(client):
    """Spawn 50 concurrent eligibility task requests and validate all complete.

    Assertions:
    - All requests return 201 (no 5xx errors)
    - Every returned task ID is unique (no duplicates / business-key collisions)
    - All 50 tasks reach a terminal status (completed, failed, or review)
    - Agent types are consistent
    - Total elapsed time stays within the contracted 60s budget
    - No data corruption (unique IDs, correct agent types)
    """
    headers = _auth_header("admin")

    async def create_task(index: int) -> dict:
        """Create a single eligibility task with a unique subscriber ID."""
        body = {
            "agent_type": "eligibility",
            "input_data": {
                "subscriber_id": f"LOAD-{index:04d}-{uuid.uuid4().hex[:8]}",
                "subscriber_first_name": f"LoadFirst{index}",
                "subscriber_last_name": f"LoadLast{index}",
                "subscriber_dob": "19900101",
                "payer_id": "LOADPAYER",
                "payer_name": "Load Test Payer",
            },
        }
        resp = await client.post(
            "/api/v1/agents/eligibility/tasks",
            json=body,
            headers=headers,
        )
        return {
            "status_code": resp.status_code,
            "body": resp.json(),
            "index": index,
            "subscriber_id": body["input_data"]["subscriber_id"],
        }

    # Fire all creation requests concurrently
    start = time.monotonic()
    results = await asyncio.gather(
        *(create_task(i) for i in range(CONCURRENT_REQUESTS)),
        return_exceptions=True,
    )

    # Separate successes from exceptions
    exceptions = [r for r in results if isinstance(r, BaseException)]
    successes = [r for r in results if not isinstance(r, BaseException)]

    assert not exceptions, (
        f"{len(exceptions)} requests raised exceptions: "
        f"{[str(e) for e in exceptions[:5]]}"
    )

    # Count 5xx errors explicitly
    five_xx = [r for r in successes if r["status_code"] >= 500]
    assert not five_xx, (
        f"{len(five_xx)} requests returned 5xx errors. "
        f"First: index={five_xx[0]['index']} status={five_xx[0]['status_code']}"
    )

    # Verify all returned 201
    non_201 = [r for r in successes if r["status_code"] != 201]
    assert not non_201, (
        f"{len(non_201)} requests did not return 201. "
        f"First failure: index={non_201[0]['index']} "
        f"status={non_201[0]['status_code']} body={non_201[0]['body']}"
    )

    # Verify unique task IDs (no duplicate business keys)
    task_ids = [r["body"]["id"] for r in successes]
    assert len(task_ids) == CONCURRENT_REQUESTS
    unique_ids = set(task_ids)
    assert len(unique_ids) == CONCURRENT_REQUESTS, (
        f"Expected {CONCURRENT_REQUESTS} unique task IDs, "
        f"got {len(unique_ids)} (duplicates found)"
    )

    # Verify unique subscriber_ids map to unique task IDs
    subscriber_ids = [r["subscriber_id"] for r in successes]
    assert len(set(subscriber_ids)) == CONCURRENT_REQUESTS, (
        "Duplicate subscriber IDs generated — randomization failure"
    )

    # ── Poll all 50 tasks until they reach terminal status ────────────
    remaining_timeout = MAX_TOTAL_SECONDS - (time.monotonic() - start)
    assert remaining_timeout > 0, (
        "Creation phase already exceeded the 60s budget before polling could begin"
    )

    completed_tasks = await asyncio.gather(
        *(
            _poll_until_terminal(
                client, "eligibility", tid, headers, timeout=remaining_timeout,
            )
            for tid in task_ids
        ),
        return_exceptions=True,
    )

    poll_exceptions = [r for r in completed_tasks if isinstance(r, BaseException)]
    assert not poll_exceptions, (
        f"{len(poll_exceptions)} tasks failed to reach terminal status: "
        f"{[str(e) for e in poll_exceptions[:5]]}"
    )

    # Contract: all tasks must complete successfully — no errors or data corruption
    completed_ids = set()
    failed_tasks = []
    for task in completed_tasks:
        assert task["agent_type"] == "eligibility", (
            f"Task {task['id']} has wrong agent_type: {task['agent_type']}"
        )
        if task["status"] != "completed":
            failed_tasks.append(
                f"task={task['id']} status={task['status']} "
                f"error={task.get('error_message', 'N/A')}"
            )
        completed_ids.add(task["id"])

    assert not failed_tasks, (
        f"{len(failed_tasks)} of {CONCURRENT_REQUESTS} tasks did not complete "
        f"successfully (contract requires all 50 to complete without errors): "
        f"{failed_tasks[:5]}"
    )

    assert len(completed_ids) == CONCURRENT_REQUESTS, (
        f"Expected {CONCURRENT_REQUESTS} unique completed task IDs, "
        f"got {len(completed_ids)}"
    )

    # Verify total time budget
    elapsed = time.monotonic() - start
    assert elapsed <= MAX_TOTAL_SECONDS, (
        f"Load test took {elapsed:.1f}s, exceeding {MAX_TOTAL_SECONDS}s budget"
    )


@pytest.mark.asyncio
async def test_concurrent_mixed_agent_creation(client):
    """Spawn concurrent requests across multiple agent types.

    Creates 10 tasks each for eligibility, scheduling, and claims (30 total)
    to verify the system handles mixed concurrent workloads without
    data corruption.
    """
    headers = _auth_header("admin")

    async def create_eligibility(index: int) -> dict:
        resp = await client.post(
            "/api/v1/agents/eligibility/tasks",
            json={
                "agent_type": "eligibility",
                "input_data": {
                    "subscriber_id": f"MIX-ELIG-{index:03d}-{uuid.uuid4().hex[:6]}",
                    "subscriber_first_name": f"MixFirst{index}",
                    "subscriber_last_name": f"MixLast{index}",
                },
            },
            headers=headers,
        )
        return {"agent": "eligibility", "status": resp.status_code, "id": resp.json().get("id")}

    async def create_scheduling(index: int) -> dict:
        resp = await client.post(
            "/api/v1/agents/scheduling/tasks",
            json={
                "agent_type": "scheduling",
                "input_data": {
                    "request_text": f"Appointment for mix patient {index}",
                    "patient_first_name": f"SchedFirst{index}",
                    "patient_last_name": f"SchedLast{index}",
                },
            },
            headers=headers,
        )
        return {"agent": "scheduling", "status": resp.status_code, "id": resp.json().get("id")}

    async def create_claims(index: int) -> dict:
        resp = await client.post(
            "/api/v1/agents/claims/tasks",
            json={
                "agent_type": "claims",
                "input_data": {
                    "subscriber_id": f"MIX-CLM-{index:03d}-{uuid.uuid4().hex[:6]}",
                    "subscriber_first_name": f"ClmFirst{index}",
                    "subscriber_last_name": f"ClmLast{index}",
                    "diagnosis_codes": ["J06.9"],
                    "procedure_codes": ["99213"],
                    "total_charge": "150.00",
                },
            },
            headers=headers,
        )
        return {"agent": "claims", "status": resp.status_code, "id": resp.json().get("id")}

    tasks_per_type = 10
    coros = []
    for i in range(tasks_per_type):
        coros.append(create_eligibility(i))
        coros.append(create_scheduling(i))
        coros.append(create_claims(i))

    start = time.monotonic()
    results = await asyncio.gather(*coros, return_exceptions=True)
    elapsed = time.monotonic() - start

    exceptions = [r for r in results if isinstance(r, BaseException)]
    successes = [r for r in results if not isinstance(r, BaseException)]

    assert not exceptions, (
        f"{len(exceptions)} requests raised exceptions: "
        f"{[str(e) for e in exceptions[:5]]}"
    )

    # No 5xx errors
    five_xx = [r for r in successes if r["status"] >= 500]
    assert not five_xx, f"{len(five_xx)} requests returned 5xx errors"

    # All should be 201
    failures = [r for r in successes if r["status"] != 201]
    assert not failures, (
        f"{len(failures)} requests failed. First: {failures[0] if failures else 'N/A'}"
    )

    # All task IDs should be unique across all agent types
    all_ids = [r["id"] for r in successes if r["id"]]
    assert len(set(all_ids)) == len(all_ids), "Duplicate task IDs found across agents"

    # Verify counts per agent type
    for agent_type in ("eligibility", "scheduling", "claims"):
        count = sum(1 for r in successes if r["agent"] == agent_type)
        assert count == tasks_per_type, (
            f"Expected {tasks_per_type} {agent_type} tasks, got {count}"
        )


@pytest.mark.asyncio
async def test_concurrent_reads_under_load(client):
    """Verify that read endpoints remain responsive under concurrent load.

    Fires 20 concurrent GET requests to the task listing endpoint.
    """
    headers = _auth_header("viewer")

    async def read_tasks() -> int:
        resp = await client.get(
            "/api/v1/agents/eligibility/tasks?limit=10&offset=0",
            headers=headers,
        )
        return resp.status_code

    start = time.monotonic()
    results = await asyncio.gather(
        *(read_tasks() for _ in range(20)),
        return_exceptions=True,
    )
    elapsed = time.monotonic() - start

    exceptions = [r for r in results if isinstance(r, BaseException)]
    assert not exceptions, f"Read requests raised exceptions: {exceptions[:3]}"

    status_codes = [r for r in results if isinstance(r, int)]
    assert all(s == 200 for s in status_codes), (
        f"Not all reads returned 200: {status_codes}"
    )
    assert elapsed < 30, f"Concurrent reads took {elapsed:.1f}s (expected < 30s)"
