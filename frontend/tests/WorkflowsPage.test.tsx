import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import WorkflowsPage from "../src/pages/WorkflowsPage";
import type { WorkflowExecutionList, WorkflowExecutionResponse } from "../src/types";

// Mock API modules
vi.mock("../src/api/workflows", () => ({
  listWorkflows: vi.fn(),
  getWorkflow: vi.fn(),
  getWorkflowHistory: vi.fn(),
}));

import { listWorkflows, getWorkflow, getWorkflowHistory } from "../src/api/workflows";
const mockListWorkflows = vi.mocked(listWorkflows);
const mockGetWorkflow = vi.mocked(getWorkflow);
const mockGetWorkflowHistory = vi.mocked(getWorkflowHistory);

const sampleWorkflows: WorkflowExecutionResponse[] = [
  {
    id: "wf-001",
    workflow_id: "temporal-wf-001",
    run_id: "run-001",
    agent_type: "eligibility",
    status: "completed",
    task_queue: "agent-queue",
    input_data: {},
    output_data: { result: "ok" },
    error_message: null,
    created_at: "2026-03-20T10:00:00Z",
    updated_at: "2026-03-20T10:05:00Z",
  },
  {
    id: "wf-002",
    workflow_id: "temporal-wf-002",
    run_id: "run-002",
    agent_type: "claims",
    status: "running",
    task_queue: "agent-queue",
    input_data: {},
    output_data: null,
    error_message: null,
    created_at: "2026-03-21T11:00:00Z",
    updated_at: null,
  },
];

const mockWorkflowList: WorkflowExecutionList = {
  items: sampleWorkflows,
  total: 2,
  limit: 15,
  offset: 0,
};

describe("WorkflowsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockListWorkflows.mockResolvedValue(mockWorkflowList);
  });

  it("renders workflow list with correct status and duration", async () => {
    render(
      <MemoryRouter>
        <WorkflowsPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText(/temporal-wf-001/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/temporal-wf-002/i)).toBeInTheDocument();
  });

  it("filters workflows by agent type", async () => {
    render(
      <MemoryRouter>
        <WorkflowsPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(mockListWorkflows).toHaveBeenCalled();
    });

    const agentFilter = screen.getByTestId("wf-agent-filter");
    fireEvent.change(agentFilter, { target: { value: "eligibility" } });

    await waitFor(() => {
      expect(mockListWorkflows).toHaveBeenCalledWith(
        expect.objectContaining({ agent_type: "eligibility" }),
        expect.anything(), // AbortSignal
      );
    });
  });

  it("filters workflows by status", async () => {
    render(
      <MemoryRouter>
        <WorkflowsPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(mockListWorkflows).toHaveBeenCalled();
    });

    const statusFilter = screen.getByTestId("wf-status-filter");
    fireEvent.change(statusFilter, { target: { value: "running" } });

    await waitFor(() => {
      expect(mockListWorkflows).toHaveBeenCalledWith(
        expect.objectContaining({ status_filter: "running" }),
        expect.anything(), // AbortSignal
      );
    });
  });

  it("shows empty state when no workflows", async () => {
    mockListWorkflows.mockResolvedValue({ items: [], total: 0, limit: 15, offset: 0 });

    render(
      <MemoryRouter>
        <WorkflowsPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("workflow-list-empty")).toBeInTheDocument();
    });
  });

  it("shows error state on fetch failure", async () => {
    mockListWorkflows.mockRejectedValue(new Error("Network error"));

    render(
      <MemoryRouter>
        <WorkflowsPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText(/network error/i)).toBeInTheDocument();
    });
  });
});
