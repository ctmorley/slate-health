import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import AuditLogViewer from "../src/components/audit/AuditLogViewer";
import type { AuditLogList } from "../src/types";

vi.mock("../src/api/audit", () => ({
  listAuditLogs: vi.fn(),
  exportAuditLogs: vi.fn(),
  fetchAuditFilterOptions: vi.fn(),
}));

import { listAuditLogs, exportAuditLogs, fetchAuditFilterOptions } from "../src/api/audit";
const mockListLogs = vi.mocked(listAuditLogs);
const mockExportLogs = vi.mocked(exportAuditLogs);
const mockFetchFilterOptions = vi.mocked(fetchAuditFilterOptions);

// Use backend-accurate action and resource_type values
const mockLogs: AuditLogList = {
  items: [
    {
      id: "log-1",
      actor_id: "user-1",
      actor_type: "user",
      action: "agent_task_created",
      resource_type: "agent_task",
      resource_id: "task-001",
      details: { agent_type: "eligibility" },
      phi_accessed: false,
      ip_address: null,
      timestamp: "2026-03-25T10:00:00Z",
    },
    {
      id: "log-2",
      actor_id: "user-1",
      actor_type: "user",
      action: "phi_accessed",
      resource_type: "patient",
      resource_id: "pat-001",
      details: { reason: "eligibility check" },
      phi_accessed: true,
      ip_address: null,
      timestamp: "2026-03-25T10:01:00Z",
    },
    {
      id: "log-3",
      actor_id: "user-2",
      actor_type: "user",
      action: "hitl_review_approved",
      resource_type: "hitl_review",
      resource_id: "rev-001",
      details: null,
      phi_accessed: false,
      ip_address: null,
      timestamp: "2026-03-25T11:00:00Z",
    },
  ],
  total: 3,
  limit: 20,
  offset: 0,
};

describe("AuditLogViewer", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockListLogs.mockResolvedValue(mockLogs);
    mockFetchFilterOptions.mockResolvedValue({
      actions: [
        "agent_task_created",
        "agent_task_cancelled",
        "hitl_review_created",
        "hitl_review_approved",
        "phi_accessed",
      ],
      resource_types: ["agent_task", "hitl_review", "patient", "claim"],
    });
  });

  it("renders filterable log entries", async () => {
    render(
      <MemoryRouter>
        <AuditLogViewer />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("audit-log-viewer")).toBeInTheDocument();
    });

    // All 3 rows rendered
    expect(screen.getByTestId("audit-row-log-1")).toBeInTheDocument();
    expect(screen.getByTestId("audit-row-log-2")).toBeInTheDocument();
    expect(screen.getByTestId("audit-row-log-3")).toBeInTheDocument();

    // Filter controls exist
    expect(screen.getByTestId("audit-search")).toBeInTheDocument();
    expect(screen.getByTestId("audit-action-filter")).toBeInTheDocument();
    expect(screen.getByTestId("audit-start-date")).toBeInTheDocument();
    expect(screen.getByTestId("audit-end-date")).toBeInTheDocument();
    expect(screen.getByTestId("audit-phi-filter")).toBeInTheDocument();
    // New: actor and resource filters
    expect(screen.getByTestId("audit-actor-filter")).toBeInTheDocument();
    expect(screen.getByTestId("audit-resource-filter")).toBeInTheDocument();
  });

  it("shows empty state when no logs", async () => {
    mockListLogs.mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 });

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

  it("displays backend-accurate action types and actor IDs", async () => {
    render(
      <MemoryRouter>
        <AuditLogViewer />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("audit-row-log-1")).toBeInTheDocument();
    });

    // Backend-accurate action values
    expect(screen.getByText("agent_task_created")).toBeInTheDocument();
    expect(screen.getByText("phi_accessed")).toBeInTheDocument();
    expect(screen.getByText("hitl_review_approved")).toBeInTheDocument();
    // Actor IDs displayed
    expect(screen.getAllByText("user-1").length).toBeGreaterThan(0);
    expect(screen.getByText("user-2")).toBeInTheDocument();
  });

  it("calls API with correct filter params when action filter changes", async () => {
    render(
      <MemoryRouter>
        <AuditLogViewer />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(mockListLogs).toHaveBeenCalledTimes(1);
    });

    // Change action filter — uses backend-matching value
    fireEvent.change(screen.getByTestId("audit-action-filter"), {
      target: { value: "agent_task_created" },
    });

    await waitFor(() => {
      expect(mockListLogs).toHaveBeenCalledWith(
        expect.objectContaining({ action: "agent_task_created" }),
        expect.anything(), // AbortSignal
      );
    });
  });

  it("sends start_time and end_time params (not start_date/end_date)", async () => {
    render(
      <MemoryRouter>
        <AuditLogViewer />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(mockListLogs).toHaveBeenCalledTimes(1);
    });

    // Set start date
    fireEvent.change(screen.getByTestId("audit-start-date"), {
      target: { value: "2026-03-20" },
    });

    await waitFor(() => {
      const lastCall = mockListLogs.mock.calls[mockListLogs.mock.calls.length - 1][0];
      expect(lastCall).toHaveProperty("start_time", "2026-03-20T00:00:00");
      expect(lastCall).not.toHaveProperty("start_date");
    });

    // Set end date
    fireEvent.change(screen.getByTestId("audit-end-date"), {
      target: { value: "2026-03-27" },
    });

    await waitFor(() => {
      const lastCall = mockListLogs.mock.calls[mockListLogs.mock.calls.length - 1][0];
      expect(lastCall).toHaveProperty("end_time", "2026-03-27T23:59:59.999999");
      expect(lastCall).not.toHaveProperty("end_date");
    });
  });

  it("has export button and shows error on export failure", async () => {
    mockExportLogs.mockRejectedValue(new Error("Server error"));

    render(
      <MemoryRouter>
        <AuditLogViewer />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText("Export")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("audit-export-button"));

    await waitFor(() => {
      expect(screen.getByTestId("audit-export-error")).toBeInTheDocument();
    });
    expect(screen.getByText(/Export failed/)).toBeInTheDocument();
  });

  it("sends resource_type filter param with backend-matching value", async () => {
    render(
      <MemoryRouter>
        <AuditLogViewer />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(mockListLogs).toHaveBeenCalledTimes(1);
    });

    fireEvent.change(screen.getByTestId("audit-resource-filter"), {
      target: { value: "hitl_review" },
    });

    await waitFor(() => {
      expect(mockListLogs).toHaveBeenCalledWith(
        expect.objectContaining({ resource_type: "hitl_review" }),
        expect.anything(), // AbortSignal
      );
    });
  });

  it("populates filter dropdowns from dynamic filter-options API", async () => {
    render(
      <MemoryRouter>
        <AuditLogViewer />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(mockFetchFilterOptions).toHaveBeenCalledTimes(1);
    });

    // Verify action dropdown contains backend-sourced values
    const actionSelect = screen.getByTestId("audit-action-filter");
    expect(actionSelect).toBeInTheDocument();
    const actionOptions = Array.from((actionSelect as HTMLSelectElement).options).map(
      (o) => o.value,
    );
    expect(actionOptions).toContain("agent_task_created");
    expect(actionOptions).toContain("hitl_review_approved");
    // Should NOT contain old mismatched values
    expect(actionOptions).not.toContain("task_created");
    expect(actionOptions).not.toContain("review_approved");

    // Verify resource_type dropdown
    const resourceSelect = screen.getByTestId("audit-resource-filter");
    const resourceOptions = Array.from((resourceSelect as HTMLSelectElement).options).map(
      (o) => o.value,
    );
    expect(resourceOptions).toContain("hitl_review");
    expect(resourceOptions).toContain("agent_task");
    // Should NOT contain old mismatched value
    expect(resourceOptions).not.toContain("review");
  });

  it("renders a different subset when date-range and action filter change together", async () => {
    // First load: full 3-row result set
    mockListLogs.mockResolvedValue(mockLogs);

    render(
      <MemoryRouter>
        <AuditLogViewer />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("audit-row-log-1")).toBeInTheDocument();
      expect(screen.getByTestId("audit-row-log-2")).toBeInTheDocument();
      expect(screen.getByTestId("audit-row-log-3")).toBeInTheDocument();
    });

    // Now configure mock to return a filtered subset when filters are applied
    const filteredLogs: AuditLogList = {
      items: [mockLogs.items[0]], // only the agent_task_created entry
      total: 1,
      limit: 20,
      offset: 0,
    };
    mockListLogs.mockResolvedValue(filteredLogs);

    // Apply both date-range and action filter
    fireEvent.change(screen.getByTestId("audit-start-date"), {
      target: { value: "2026-03-25" },
    });
    fireEvent.change(screen.getByTestId("audit-action-filter"), {
      target: { value: "agent_task_created" },
    });

    // The component should re-fetch and render only the filtered row
    await waitFor(() => {
      expect(screen.getByTestId("audit-row-log-1")).toBeInTheDocument();
      expect(screen.queryByTestId("audit-row-log-2")).not.toBeInTheDocument();
      expect(screen.queryByTestId("audit-row-log-3")).not.toBeInTheDocument();
    });

    // Verify the API was called with both filter parameters
    expect(mockListLogs).toHaveBeenCalledWith(
      expect.objectContaining({
        action: "agent_task_created",
        start_time: "2026-03-25T00:00:00",
      }),
      expect.anything(), // AbortSignal
    );
  });

  it("sends search param to API instead of filtering client-side", async () => {
    render(
      <MemoryRouter>
        <AuditLogViewer />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(mockListLogs).toHaveBeenCalledTimes(1);
    });

    // Type in the search box
    fireEvent.change(screen.getByTestId("audit-search"), {
      target: { value: "agent_task" },
    });

    // Wait for the debounce (300ms) + re-render to propagate
    await waitFor(
      () => {
        expect(mockListLogs).toHaveBeenCalledWith(
          expect.objectContaining({ search: "agent_task" }),
          expect.anything(), // AbortSignal
        );
      },
      { timeout: 2000 },
    );
  });
});
