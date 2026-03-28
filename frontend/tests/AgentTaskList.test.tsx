import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import AgentTaskList from "../src/components/agents/AgentTaskList";
import type { AgentTaskList as AgentTaskListType } from "../src/types";

// Mock the agents API module
vi.mock("../src/api/agents", () => ({
  listTasks: vi.fn(),
}));

import { listTasks } from "../src/api/agents";
const mockListTasks = vi.mocked(listTasks);

function makeTasks(count: number): AgentTaskListType {
  const statuses = ["pending", "running", "completed", "failed", "review", "cancelled"] as const;
  return {
    items: Array.from({ length: count }, (_, i) => ({
      id: `id-${i}`,
      task_id: `task-${String(i).padStart(12, "0")}`,
      agent_type: "eligibility" as const,
      status: statuses[i % statuses.length],
      input_data: { subscriber_id: `SUB-${i}` },
      output_data: null,
      error_message: null,
      confidence_score: i % 2 === 0 ? 0.85 : null,
      workflow_execution_id: null,
      patient_id: `PAT-${i}`,
      organization_id: null,
      created_at: new Date(Date.now() - i * 86400000).toISOString(),
      updated_at: null,
    })),
    total: count,
    limit: 10,
    offset: 0,
  };
}

describe("AgentTaskList", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders 10 tasks with correct status badges", async () => {
    mockListTasks.mockResolvedValue(makeTasks(10));
    const onSelect = vi.fn();

    render(
      <MemoryRouter>
        <AgentTaskList agentType="eligibility" onSelectTask={onSelect} />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("agent-task-list")).toBeInTheDocument();
    });

    // Check status badges exist — multiple tasks may share the same status
    expect(screen.getAllByTestId("status-badge-pending").length).toBeGreaterThan(0);
    expect(screen.getAllByTestId("status-badge-running").length).toBeGreaterThan(0);
    expect(screen.getAllByTestId("status-badge-completed").length).toBeGreaterThan(0);
    expect(screen.getAllByTestId("status-badge-failed").length).toBeGreaterThan(0);
    expect(screen.getAllByTestId("status-badge-review").length).toBeGreaterThan(0);
    expect(screen.getAllByTestId("status-badge-cancelled").length).toBeGreaterThan(0);
  });

  it("shows empty state when no tasks", async () => {
    mockListTasks.mockResolvedValue({ items: [], total: 0, limit: 10, offset: 0 });
    const onSelect = vi.fn();

    render(
      <MemoryRouter>
        <AgentTaskList agentType="eligibility" onSelectTask={onSelect} />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("task-list-empty")).toBeInTheDocument();
    });
    expect(screen.getByText("No tasks found")).toBeInTheDocument();
  });

  it("calls onSelectTask when clicking a task row", async () => {
    const tasks = makeTasks(3);
    mockListTasks.mockResolvedValue(tasks);
    const onSelect = vi.fn();

    render(
      <MemoryRouter>
        <AgentTaskList agentType="eligibility" onSelectTask={onSelect} />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("task-row-id-0")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("task-row-id-0"));
    expect(onSelect).toHaveBeenCalledWith(tasks.items[0]);
  });

  it("shows error state on API failure", async () => {
    mockListTasks.mockRejectedValue(new Error("Network error"));
    const onSelect = vi.fn();

    render(
      <MemoryRouter>
        <AgentTaskList agentType="eligibility" onSelectTask={onSelect} />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("task-list-error")).toBeInTheDocument();
    });
    expect(screen.getByText("Network error")).toBeInTheDocument();
  });

  it("has search input, status filter, and date filters", async () => {
    mockListTasks.mockResolvedValue(makeTasks(5));
    const onSelect = vi.fn();

    render(
      <MemoryRouter>
        <AgentTaskList agentType="eligibility" onSelectTask={onSelect} />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("task-search-input")).toBeInTheDocument();
      expect(screen.getByTestId("task-status-filter")).toBeInTheDocument();
      expect(screen.getByTestId("task-start-date")).toBeInTheDocument();
      expect(screen.getByTestId("task-end-date")).toBeInTheDocument();
    });
  });

  it("sends status_filter param (not status) to backend API", async () => {
    mockListTasks.mockResolvedValue(makeTasks(5));
    const onSelect = vi.fn();

    render(
      <MemoryRouter>
        <AgentTaskList agentType="eligibility" onSelectTask={onSelect} />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(mockListTasks).toHaveBeenCalledTimes(1);
    });

    // Change status filter
    fireEvent.change(screen.getByTestId("task-status-filter"), {
      target: { value: "completed" },
    });

    await waitFor(() => {
      const lastCall = mockListTasks.mock.calls[mockListTasks.mock.calls.length - 1];
      const params = lastCall[1];
      expect(params).toHaveProperty("status_filter", "completed");
      expect(params).not.toHaveProperty("status");
    });
  });
});
