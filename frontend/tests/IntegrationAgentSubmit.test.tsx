import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import AgentPage from "../src/pages/AgentPage";

// Mock API modules
vi.mock("../src/api/agents", () => ({
  createTask: vi.fn(),
  listTasks: vi.fn(),
  getTask: vi.fn(),
}));

// Mock hooks/useWebSocket to avoid WS context issues
vi.mock("../src/hooks/useWebSocket", () => ({
  useWebSocket: () => ({ isConnected: false, lastMessage: null }),
}));

import { createTask, listTasks } from "../src/api/agents";
const mockCreateTask = vi.mocked(createTask);
const mockListTasks = vi.mocked(listTasks);

describe("Integration: submit new eligibility task via form", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockListTasks.mockResolvedValue({
      items: [],
      total: 0,
      limit: 10,
      offset: 0,
    });
  });

  it("fills form and verifies API called with correct payload", async () => {
    mockCreateTask.mockResolvedValue({
      id: "id-new",
      task_id: "task-new",
      agent_type: "eligibility",
      status: "pending",
      input_data: {},
      output_data: null,
      error_message: null,
      confidence_score: null,
      workflow_execution_id: null,
      patient_id: null,
      organization_id: null,
      created_at: new Date().toISOString(),
      updated_at: null,
    });

    const user = userEvent.setup();

    render(
      <MemoryRouter initialEntries={["/agents/eligibility"]}>
        <Routes>
          <Route path="/agents/:agentType" element={<AgentPage />} />
        </Routes>
      </MemoryRouter>,
    );

    // Wait for initial task list
    await waitFor(() => {
      expect(screen.getByTestId("new-task-button")).toBeInTheDocument();
    });

    // Click "New Task"
    fireEvent.click(screen.getByTestId("new-task-button"));

    // Fill in form fields
    await waitFor(() => {
      expect(screen.getByTestId("new-task-form")).toBeInTheDocument();
    });

    await user.type(screen.getByTestId("field-subscriber_id"), "MEM999");
    await user.type(screen.getByTestId("field-subscriber_first_name"), "Jane");
    await user.type(screen.getByTestId("field-subscriber_last_name"), "Smith");

    // Submit
    fireEvent.click(screen.getByText("Create Task"));

    // Verify API was called correctly
    await waitFor(() => {
      expect(mockCreateTask).toHaveBeenCalledWith("eligibility", {
        input_data: {
          subscriber_id: "MEM999",
          subscriber_first_name: "Jane",
          subscriber_last_name: "Smith",
        },
      });
    });
  });
});
