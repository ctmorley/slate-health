import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import NewTaskForm from "../src/components/agents/NewTaskForm";

vi.mock("../src/api/agents", () => ({
  createTask: vi.fn(),
}));

import { createTask } from "../src/api/agents";
const mockCreateTask = vi.mocked(createTask);

describe("NewTaskForm", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders eligibility form fields", () => {
    render(
      <MemoryRouter>
        <NewTaskForm
          agentType="eligibility"
          onCreated={vi.fn()}
          onCancel={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(screen.getByTestId("new-task-form")).toBeInTheDocument();
    expect(screen.getByTestId("field-subscriber_id")).toBeInTheDocument();
    expect(screen.getByTestId("field-subscriber_first_name")).toBeInTheDocument();
    expect(screen.getByTestId("field-subscriber_last_name")).toBeInTheDocument();
  });

  it("validates required fields and shows errors", async () => {
    const onCreated = vi.fn();
    render(
      <MemoryRouter>
        <NewTaskForm
          agentType="eligibility"
          onCreated={onCreated}
          onCancel={vi.fn()}
        />
      </MemoryRouter>,
    );

    // Submit without filling required fields
    fireEvent.click(screen.getByText("Create Task"));

    await waitFor(() => {
      expect(screen.getByTestId("error-subscriber_id")).toBeInTheDocument();
    });

    expect(screen.getByText("Subscriber ID is required")).toBeInTheDocument();
    expect(screen.getByText("First Name is required")).toBeInTheDocument();
    expect(screen.getByText("Last Name is required")).toBeInTheDocument();

    // createTask should NOT have been called
    expect(mockCreateTask).not.toHaveBeenCalled();
    expect(onCreated).not.toHaveBeenCalled();
  });

  it("submits form with correct payload for eligibility", async () => {
    mockCreateTask.mockResolvedValue({
      id: "id-1",
      task_id: "task-1",
      agent_type: "eligibility",
      status: "pending",
      input_data: {},
      output_data: null,
      error_message: null,
      confidence_score: null,
      workflow_execution_id: null,
      patient_id: null,
      organization_id: null,
      created_at: null,
      updated_at: null,
    });

    const onCreated = vi.fn();
    const user = userEvent.setup();

    render(
      <MemoryRouter>
        <NewTaskForm
          agentType="eligibility"
          onCreated={onCreated}
          onCancel={vi.fn()}
        />
      </MemoryRouter>,
    );

    await user.type(screen.getByTestId("field-subscriber_id"), "MEM123");
    await user.type(screen.getByTestId("field-subscriber_first_name"), "John");
    await user.type(screen.getByTestId("field-subscriber_last_name"), "Doe");

    fireEvent.click(screen.getByText("Create Task"));

    await waitFor(() => {
      expect(mockCreateTask).toHaveBeenCalledWith("eligibility", {
        input_data: {
          subscriber_id: "MEM123",
          subscriber_first_name: "John",
          subscriber_last_name: "Doe",
        },
      });
    });

    await waitFor(() => {
      expect(onCreated).toHaveBeenCalled();
    });
  });

  it("renders different fields for scheduling agent", () => {
    render(
      <MemoryRouter>
        <NewTaskForm
          agentType="scheduling"
          onCreated={vi.fn()}
          onCancel={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(screen.getByTestId("field-request_text")).toBeInTheDocument();
    expect(screen.getByTestId("field-patient_first_name")).toBeInTheDocument();
  });

  it("shows error on API failure", async () => {
    mockCreateTask.mockRejectedValue(new Error("Server error"));
    const user = userEvent.setup();

    render(
      <MemoryRouter>
        <NewTaskForm
          agentType="credentialing"
          onCreated={vi.fn()}
          onCancel={vi.fn()}
        />
      </MemoryRouter>,
    );

    await user.type(screen.getByTestId("field-provider_npi"), "1234567890");
    fireEvent.click(screen.getByText("Create Task"));

    await waitFor(() => {
      expect(screen.getByTestId("submit-error")).toBeInTheDocument();
    });
    expect(screen.getByText("Server error")).toBeInTheDocument();
  });
});
