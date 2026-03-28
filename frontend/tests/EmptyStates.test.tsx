import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import AgentTaskList from "../src/components/agents/AgentTaskList";
import ReviewQueue from "../src/components/reviews/ReviewQueue";
import WorkflowList from "../src/components/workflows/WorkflowList";
import AuditLogViewer from "../src/components/audit/AuditLogViewer";

vi.mock("../src/api/agents", () => ({
  listTasks: vi.fn(),
}));

vi.mock("../src/api/audit", () => ({
  listAuditLogs: vi.fn(),
  exportAuditLogs: vi.fn(),
  fetchAuditFilterOptions: vi.fn().mockResolvedValue({ actions: [], resource_types: [] }),
}));

import { listTasks } from "../src/api/agents";
import { listAuditLogs } from "../src/api/audit";
const mockListTasks = vi.mocked(listTasks);
const mockListAuditLogs = vi.mocked(listAuditLogs);

describe("Empty states for all data components", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("AgentTaskList shows empty state", async () => {
    mockListTasks.mockResolvedValue({ items: [], total: 0, limit: 10, offset: 0 });

    render(
      <MemoryRouter>
        <AgentTaskList agentType="eligibility" onSelectTask={vi.fn()} />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("task-list-empty")).toBeInTheDocument();
    });
    expect(screen.getByText("No tasks found")).toBeInTheDocument();
  });

  it("ReviewQueue shows empty state", () => {
    render(
      <ReviewQueue reviews={[]} loading={false} onSelectReview={vi.fn()} />,
    );

    expect(screen.getByTestId("review-queue-empty")).toBeInTheDocument();
    expect(screen.getByText("No reviews in queue")).toBeInTheDocument();
  });

  it("WorkflowList shows empty state", () => {
    render(
      <WorkflowList workflows={[]} loading={false} onSelectWorkflow={vi.fn()} />,
    );

    expect(screen.getByTestId("workflow-list-empty")).toBeInTheDocument();
    expect(screen.getByText("No workflow executions found")).toBeInTheDocument();
  });

  it("AuditLogViewer shows empty state", async () => {
    mockListAuditLogs.mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 });

    render(
      <MemoryRouter>
        <AuditLogViewer />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("audit-empty")).toBeInTheDocument();
    });
    expect(screen.getByText("No audit log entries found")).toBeInTheDocument();
  });
});
