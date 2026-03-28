import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import WorkflowList from "../src/components/workflows/WorkflowList";
import type { WorkflowExecutionResponse } from "../src/types";

const mockWorkflows: WorkflowExecutionResponse[] = [
  {
    id: "wf-1",
    workflow_id: "eligibility-wf-001234567890",
    run_id: "run-abc123",
    agent_type: "eligibility",
    status: "completed",
    task_queue: "agent-tasks",
    input_data: { subscriber_id: "MEM123" },
    output_data: { coverage_active: true },
    error_message: null,
    created_at: "2026-03-25T10:00:00Z",
    updated_at: "2026-03-25T10:02:30Z",
  },
  {
    id: "wf-2",
    workflow_id: "claims-wf-001234567890",
    run_id: "run-def456",
    agent_type: "claims",
    status: "running",
    task_queue: "agent-tasks",
    input_data: {},
    output_data: null,
    error_message: null,
    created_at: "2026-03-25T09:50:00Z",
    updated_at: null,
  },
  {
    id: "wf-3",
    workflow_id: "prior-auth-wf-001234567890",
    run_id: null,
    agent_type: "prior_auth",
    status: "failed",
    task_queue: null,
    input_data: {},
    output_data: null,
    error_message: "Timeout exceeded",
    created_at: "2026-03-24T15:00:00Z",
    updated_at: "2026-03-24T15:10:00Z",
  },
];

describe("WorkflowList", () => {
  it("renders executions with status and duration", () => {
    render(
      <WorkflowList
        workflows={mockWorkflows}
        loading={false}
        onSelectWorkflow={vi.fn()}
      />,
    );

    expect(screen.getByTestId("workflow-list")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-row-wf-1")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-row-wf-2")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-row-wf-3")).toBeInTheDocument();

    // Status badges
    expect(screen.getByTestId("wf-status-completed")).toBeInTheDocument();
    expect(screen.getByTestId("wf-status-running")).toBeInTheDocument();
    expect(screen.getByTestId("wf-status-failed")).toBeInTheDocument();

    // Agent labels
    expect(screen.getByText("Eligibility")).toBeInTheDocument();
    expect(screen.getByText("Claims & Billing")).toBeInTheDocument();
    expect(screen.getByText("Prior Auth")).toBeInTheDocument();
  });

  it("shows empty state when no workflows", () => {
    render(
      <WorkflowList
        workflows={[]}
        loading={false}
        onSelectWorkflow={vi.fn()}
      />,
    );

    expect(screen.getByTestId("workflow-list-empty")).toBeInTheDocument();
  });

  it("shows loading state", () => {
    render(
      <WorkflowList
        workflows={[]}
        loading={true}
        onSelectWorkflow={vi.fn()}
      />,
    );

    expect(screen.getByTestId("workflow-list-loading")).toBeInTheDocument();
  });

  it("calls onSelectWorkflow on row click", () => {
    const onSelect = vi.fn();

    render(
      <WorkflowList
        workflows={mockWorkflows}
        loading={false}
        onSelectWorkflow={onSelect}
      />,
    );

    fireEvent.click(screen.getByTestId("workflow-row-wf-1"));
    expect(onSelect).toHaveBeenCalledWith(mockWorkflows[0]);
  });

  it("displays workflow ID prefix", () => {
    render(
      <WorkflowList
        workflows={mockWorkflows}
        loading={false}
        onSelectWorkflow={vi.fn()}
      />,
    );

    expect(screen.getByText("eligibility-wf-0")).toBeInTheDocument();
  });
});
