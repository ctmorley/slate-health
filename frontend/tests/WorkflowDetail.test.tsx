import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import WorkflowDetail from "../src/components/workflows/WorkflowDetail";
import type { WorkflowExecutionResponse, WorkflowHistoryResponse } from "../src/types";

// Mock API modules
vi.mock("../src/api/workflows", () => ({
  getWorkflow: vi.fn(),
  getWorkflowHistory: vi.fn(),
}));

import { getWorkflow, getWorkflowHistory } from "../src/api/workflows";
const mockGetWorkflow = vi.mocked(getWorkflow);
const mockGetWorkflowHistory = vi.mocked(getWorkflowHistory);

const sampleWorkflow: WorkflowExecutionResponse = {
  id: "wf-001",
  workflow_id: "temporal-wf-001",
  run_id: "run-001",
  agent_type: "eligibility",
  status: "completed",
  task_queue: "agent-queue",
  input_data: { test: true },
  output_data: { result: "ok" },
  error_message: null,
  created_at: "2026-03-20T10:00:00Z",
  updated_at: "2026-03-20T10:05:00Z",
};

const sampleHistory: WorkflowHistoryResponse = {
  workflow_id: "temporal-wf-001",
  events: [
    {
      event_id: 1,
      event_type: "WorkflowExecutionStarted",
      timestamp: "2026-03-20T10:00:00Z",
      details: { task_queue: "agent-queue" },
    },
    {
      event_id: 2,
      event_type: "ActivityTaskCompleted",
      timestamp: "2026-03-20T10:03:00Z",
      details: { activity: "run_agent" },
    },
    {
      event_id: 3,
      event_type: "WorkflowExecutionCompleted",
      timestamp: "2026-03-20T10:05:00Z",
      details: {},
    },
  ],
};

describe("WorkflowDetail", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetWorkflow.mockResolvedValue(sampleWorkflow);
    mockGetWorkflowHistory.mockResolvedValue(sampleHistory);
  });

  it("renders workflow header with status and agent type", async () => {
    render(<WorkflowDetail workflowId="wf-001" onBack={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByTestId("workflow-detail")).toBeInTheDocument();
    });

    expect(screen.getByText("temporal-wf-001")).toBeInTheDocument();
    expect(screen.getByText("Eligibility")).toBeInTheDocument();
    expect(screen.getByText("completed")).toBeInTheDocument();
  });

  it("renders event timeline with expandable activity details", async () => {
    render(<WorkflowDetail workflowId="wf-001" onBack={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByTestId("event-timeline")).toBeInTheDocument();
    });

    // All 3 events should be visible
    expect(screen.getByTestId("event-1")).toBeInTheDocument();
    expect(screen.getByTestId("event-2")).toBeInTheDocument();
    expect(screen.getByTestId("event-3")).toBeInTheDocument();

    // Event 1 has details — toggle to expand
    const toggleBtn = screen.getByTestId("toggle-event-1");
    fireEvent.click(toggleBtn);

    await waitFor(() => {
      expect(screen.getByText(/"task_queue"/)).toBeInTheDocument();
    });
  });

  it("calls onBack when back button clicked", async () => {
    const onBack = vi.fn();
    render(<WorkflowDetail workflowId="wf-001" onBack={onBack} />);

    await waitFor(() => {
      expect(screen.getByTestId("workflow-detail")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Back to list"));
    expect(onBack).toHaveBeenCalled();
  });

  it("shows error state on fetch failure", async () => {
    mockGetWorkflow.mockRejectedValue(new Error("Not found"));

    render(<WorkflowDetail workflowId="wf-999" onBack={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByTestId("workflow-detail-error")).toBeInTheDocument();
    });
  });

  it("shows no events message when history is empty", async () => {
    mockGetWorkflowHistory.mockResolvedValue({ workflow_id: "temporal-wf-001", events: [] });

    render(<WorkflowDetail workflowId="wf-001" onBack={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByTestId("no-events")).toBeInTheDocument();
    });
  });
});
