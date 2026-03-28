import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import AgentTaskDetail from "../src/components/agents/AgentTaskDetail";
import type { AgentTaskResponse } from "../src/types";

vi.mock("../src/api/agents", () => ({
  getTask: vi.fn(),
}));

vi.mock("../src/api/reviews", () => ({
  listReviews: vi.fn(),
}));

vi.mock("../src/api/audit", () => ({
  listAuditLogs: vi.fn(),
}));

import { getTask } from "../src/api/agents";
import { listReviews } from "../src/api/reviews";
import { listAuditLogs } from "../src/api/audit";

const mockGetTask = vi.mocked(getTask);
const mockListReviews = vi.mocked(listReviews);
const mockListAuditLogs = vi.mocked(listAuditLogs);

const mockTask: AgentTaskResponse = {
  id: "id-001",
  task_id: "task-000000000001",
  agent_type: "eligibility",
  status: "completed",
  input_data: { subscriber_id: "MEM123", subscriber_first_name: "John" },
  output_data: { coverage_active: true, confidence: 0.92 },
  error_message: null,
  confidence_score: 0.92,
  workflow_execution_id: "wf-001",
  patient_id: "PAT-001",
  organization_id: "ORG-001",
  created_at: "2026-03-25T10:00:00Z",
  updated_at: "2026-03-25T10:05:00Z",
};

describe("AgentTaskDetail", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockListReviews.mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 });
    mockListAuditLogs.mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 });
  });

  it("renders all sections (input, output, timeline, audit)", async () => {
    mockGetTask.mockResolvedValue(mockTask);
    mockListAuditLogs.mockResolvedValue({
      items: [
        {
          id: "audit-1",
          actor_id: "user-1",
          actor_type: "user",
          action: "task_created",
          resource_type: "agent_task",
          resource_id: "id-001",
          details: null,
          phi_accessed: false,
          ip_address: null,
          timestamp: "2026-03-25T10:00:00Z",
        },
      ],
      total: 1,
      limit: 50,
      offset: 0,
    });
    const onBack = vi.fn();

    render(
      <MemoryRouter>
        <AgentTaskDetail
          agentType="eligibility"
          taskId="task-000000000001"
          onBack={onBack}
        />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("agent-task-detail")).toBeInTheDocument();
    });

    // Header info — task_id.slice(0, 12) = "task-0000000"
    expect(screen.getByText("Task task-0000000")).toBeInTheDocument();
    // "Completed" appears in both the status badge and the timeline
    expect(screen.getAllByText("Completed").length).toBeGreaterThan(0);

    // Input / Output JSON blocks
    expect(screen.getByTestId("json-block-Input Data")).toBeInTheDocument();
    expect(screen.getByTestId("json-block-Output Data")).toBeInTheDocument();

    // Timeline
    expect(screen.getByTestId("status-timeline")).toBeInTheDocument();
    expect(screen.getByText("Created")).toBeInTheDocument();

    // Audit entries section
    expect(screen.getByTestId("audit-entries-section")).toBeInTheDocument();
  });

  it("shows error state when task fetch fails", async () => {
    mockGetTask.mockRejectedValue(new Error("Not found"));
    const onBack = vi.fn();

    render(
      <MemoryRouter>
        <AgentTaskDetail
          agentType="eligibility"
          taskId="invalid-id"
          onBack={onBack}
        />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("task-detail-error")).toBeInTheDocument();
    });
    expect(screen.getByText("Not found")).toBeInTheDocument();
  });

  it("displays confidence score and workflow link", async () => {
    mockGetTask.mockResolvedValue(mockTask);
    const onBack = vi.fn();

    render(
      <MemoryRouter>
        <AgentTaskDetail
          agentType="eligibility"
          taskId="task-000000000001"
          onBack={onBack}
        />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("agent-task-detail")).toBeInTheDocument();
    });

    expect(screen.getByText("92%")).toBeInTheDocument();
    expect(screen.getByText("wf-001")).toBeInTheDocument();
  });

  it("shows linked review when one exists for the task", async () => {
    mockGetTask.mockResolvedValue(mockTask);
    mockListReviews.mockResolvedValue({
      items: [
        {
          id: "rev-100",
          task_id: "id-001",
          reviewer_id: null,
          status: "pending",
          reason: "Ambiguous coverage",
          agent_decision: { agent_type: "eligibility" },
          confidence_score: 0.55,
          reviewer_notes: null,
          decided_at: null,
          created_at: "2026-03-25T10:02:00Z",
          updated_at: null,
        },
      ],
      total: 1,
      limit: 50,
      offset: 0,
    });
    const onBack = vi.fn();

    render(
      <MemoryRouter>
        <AgentTaskDetail
          agentType="eligibility"
          taskId="task-000000000001"
          onBack={onBack}
        />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("agent-task-detail")).toBeInTheDocument();
    });

    await waitFor(() => {
      expect(screen.getByTestId("linked-review-section")).toBeInTheDocument();
    });
    expect(screen.getByText("Ambiguous coverage")).toBeInTheDocument();
  });
});
