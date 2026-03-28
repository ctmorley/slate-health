import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import DashboardPage from "../src/pages/DashboardPage";
import type { DashboardSummary, AgentMetrics } from "../src/types";

// Mock auth context — returns a pre-authenticated user
vi.mock("../src/contexts/AuthContext", () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => children,
  useAuthContext: () => ({
    user: { id: "u1", email: "admin@test.com", full_name: "Admin", roles: ["admin"], organization_id: null, last_login: null, is_active: true },
    isAuthenticated: true,
    isLoading: false,
    error: null,
    login: vi.fn(),
    handleAuthCallback: vi.fn(),
    logout: vi.fn(),
  }),
}));

// Mock useAuth (thin wrapper)
vi.mock("../src/hooks/useAuth", () => ({
  useAuth: () => ({
    user: { id: "u1", email: "admin@test.com", full_name: "Admin", roles: ["admin"], organization_id: null, last_login: null, is_active: true },
    isAuthenticated: true,
    isLoading: false,
    error: null,
    login: vi.fn(),
    handleAuthCallback: vi.fn(),
    logout: vi.fn(),
  }),
}));

// Mock WebSocket context — single connection, no duplicates
vi.mock("../src/contexts/WebSocketContext", () => ({
  WebSocketProvider: ({ children }: { children: React.ReactNode }) => children,
  useWebSocketContext: () => ({
    isConnected: true,
    lastMessage: null,
    subscribe: () => () => {},
  }),
}));

// Capture the onMessage callback so tests can simulate WS events
let capturedOnMessage: ((msg: unknown) => void) | undefined;
vi.mock("../src/hooks/useWebSocket", () => ({
  useWebSocket: (opts?: { onMessage?: (msg: unknown) => void }) => {
    capturedOnMessage = opts?.onMessage;
    return { isConnected: true, lastMessage: null };
  },
}));

const mockSummary: DashboardSummary = {
  total_tasks: 42,
  pending: 5,
  running: 3,
  completed: 28,
  failed: 2,
  in_review: 4,
  cancelled: 0,
  agents: [
    { agent_type: "eligibility", total_tasks: 15, pending: 2, running: 1, completed: 10, failed: 1, in_review: 1, cancelled: 0, avg_confidence: 0.92 },
    { agent_type: "scheduling", total_tasks: 8, pending: 1, running: 0, completed: 6, failed: 0, in_review: 1, cancelled: 0, avg_confidence: 0.88 },
    { agent_type: "claims", total_tasks: 10, pending: 1, running: 1, completed: 7, failed: 1, in_review: 0, cancelled: 0, avg_confidence: 0.85 },
    { agent_type: "prior_auth", total_tasks: 5, pending: 1, running: 1, completed: 2, failed: 0, in_review: 1, cancelled: 0, avg_confidence: 0.78 },
    { agent_type: "credentialing", total_tasks: 2, pending: 0, running: 0, completed: 1, failed: 0, in_review: 1, cancelled: 0, avg_confidence: 0.95 },
    { agent_type: "compliance", total_tasks: 2, pending: 0, running: 0, completed: 2, failed: 0, in_review: 0, cancelled: 0, avg_confidence: 0.91 },
  ],
  recent_tasks: [
    { id: "t1", task_id: "t1", agent_type: "eligibility", status: "completed", confidence_score: 0.95, created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
    { id: "t2", task_id: "t2", agent_type: "claims", status: "running", confidence_score: null, created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
    { id: "t3", task_id: "t3", agent_type: "prior_auth", status: "review", confidence_score: 0.45, created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
  ],
};

function makeMockMetrics(agentType: string): AgentMetrics {
  return {
    agent_type: agentType as AgentMetrics["agent_type"],
    total_tasks: 7,
    completed: 5,
    failed: 1,
    avg_confidence: 0.88,
    avg_processing_time_seconds: 10.0,
    tasks_by_day: [
      { date: "2026-03-21", count: 1 },
      { date: "2026-03-22", count: 2 },
      { date: "2026-03-23", count: 0 },
      { date: "2026-03-24", count: 1 },
      { date: "2026-03-25", count: 1 },
      { date: "2026-03-26", count: 1 },
      { date: "2026-03-27", count: 1 },
    ],
  };
}

// Mock dashboard API
vi.mock("../src/api/dashboard", () => ({
  fetchDashboardSummary: vi.fn(),
  fetchAgentMetrics: vi.fn(),
}));

import { fetchDashboardSummary, fetchAgentMetrics } from "../src/api/dashboard";
const mockFetchSummary = vi.mocked(fetchDashboardSummary);
const mockFetchMetrics = vi.mocked(fetchAgentMetrics);

describe("DashboardPage integration", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetchSummary.mockResolvedValue(mockSummary);
    mockFetchMetrics.mockImplementation((agentType) =>
      Promise.resolve(makeMockMetrics(agentType)),
    );
  });

  it("loads and renders all dashboard sections with API data", async () => {
    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    );

    // Verify APIs were called
    expect(mockFetchSummary).toHaveBeenCalledTimes(1);

    // Wait for data to load
    await waitFor(() => {
      expect(screen.getByText("Dashboard")).toBeInTheDocument();
    });

    // Summary stats bar
    await waitFor(() => {
      expect(screen.getByText("42")).toBeInTheDocument(); // total
    });

    // Agent status cards — verify the grid has 6 agent cards via data-testid
    expect(screen.getByTestId("agent-status-cards")).toBeInTheDocument();
    expect(screen.getByTestId("agent-card-eligibility")).toBeInTheDocument();
    expect(screen.getByTestId("agent-card-scheduling")).toBeInTheDocument();
    expect(screen.getByTestId("agent-card-claims")).toBeInTheDocument();
    expect(screen.getByTestId("agent-card-prior_auth")).toBeInTheDocument();
    expect(screen.getByTestId("agent-card-credentialing")).toBeInTheDocument();
    expect(screen.getByTestId("agent-card-compliance")).toBeInTheDocument();

    // Recent activity section
    expect(screen.getByTestId("recent-activity")).toBeInTheDocument();
  });

  it("fetches metrics for all 6 agent types (not just eligibility)", async () => {
    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText("Dashboard")).toBeInTheDocument();
    });

    // Verify all 6 agent metrics were fetched
    expect(mockFetchMetrics).toHaveBeenCalledTimes(6);
    expect(mockFetchMetrics).toHaveBeenCalledWith("eligibility");
    expect(mockFetchMetrics).toHaveBeenCalledWith("scheduling");
    expect(mockFetchMetrics).toHaveBeenCalledWith("claims");
    expect(mockFetchMetrics).toHaveBeenCalledWith("prior_auth");
    expect(mockFetchMetrics).toHaveBeenCalledWith("credentialing");
    expect(mockFetchMetrics).toHaveBeenCalledWith("compliance");
  });

  it("shows error state when API call fails", async () => {
    mockFetchSummary.mockRejectedValue(new Error("Server error"));

    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText("Server error")).toBeInTheDocument();
    });
  });

  it("refreshes chart data (not just activity feed) on WS task_status_changed event", async () => {
    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    );

    // Wait for initial load
    await waitFor(() => {
      expect(screen.getByText("Dashboard")).toBeInTheDocument();
    });

    // Clear call counts after initial load
    mockFetchSummary.mockClear();
    mockFetchMetrics.mockClear();

    // Simulate a WebSocket event
    expect(capturedOnMessage).toBeDefined();
    await act(async () => {
      capturedOnMessage!({
        event: "task_status_changed",
        data: { task_id: "t-new", agent_type: "eligibility", status: "completed" },
      });
    });

    // The WS handler should trigger a refetch of both summary AND agent metrics
    // (for chart data), not just prepend to the activity feed.
    await waitFor(() => {
      expect(mockFetchSummary).toHaveBeenCalled();
    });
    await waitFor(() => {
      // All 6 agent metrics should be refetched for chart data
      expect(mockFetchMetrics).toHaveBeenCalledTimes(6);
    });
  });

  it("deduplicates realtime feed entries by task_id", async () => {
    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText("Dashboard")).toBeInTheDocument();
    });

    expect(capturedOnMessage).toBeDefined();

    // Send two events for the same task_id with different statuses
    await act(async () => {
      capturedOnMessage!({
        event: "task_status_changed",
        data: { task_id: "t1", agent_type: "eligibility", status: "running" },
      });
    });

    await act(async () => {
      capturedOnMessage!({
        event: "task_status_changed",
        data: { task_id: "t1", agent_type: "eligibility", status: "completed" },
      });
    });

    // The recent activity feed should only contain one entry for t1 (the latest),
    // not two duplicate rows. We started with 3 tasks (t1, t2, t3) from the API.
    // After two WS events for t1, dedupe should keep exactly one t1 entry.
    const activityContainer = screen.getByTestId("recent-activity");
    const t1Items = activityContainer.querySelectorAll("[data-task-id='t1']");
    expect(t1Items).toHaveLength(1);
  });

  it("shows loading spinner while data is being fetched", () => {
    // Never resolve the promise to keep it loading
    mockFetchSummary.mockReturnValue(new Promise(() => {}));

    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    );

    // Loading spinner should be present (the animate-spin div)
    const spinner = document.querySelector(".animate-spin");
    expect(spinner).toBeInTheDocument();
  });

  it("shows warning when some agent metrics fail to load", async () => {
    // Make eligibility and claims metrics fail, others succeed
    mockFetchMetrics.mockImplementation((agentType: string) => {
      if (agentType === "eligibility" || agentType === "claims") {
        return Promise.reject(new Error("Service unavailable"));
      }
      return Promise.resolve(makeMockMetrics(agentType));
    });

    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    );

    // Wait for data to load
    await waitFor(() => {
      expect(screen.getByText("Dashboard")).toBeInTheDocument();
    });

    // The partial metrics warning should be visible
    await waitFor(() => {
      const warning = screen.getByTestId("metrics-partial-warning");
      expect(warning).toBeInTheDocument();
      expect(warning).toHaveTextContent("Eligibility");
      expect(warning).toHaveTextContent("Claims & Billing");
      expect(warning).toHaveTextContent("Incomplete chart data");
    });
  });

  it("retry button re-fetches failed agent metrics and clears warning on success", async () => {
    const user = userEvent.setup();

    // Phase tracking: "initial" = fail all eligibility calls, "retry" = succeed
    let phase = "initial";

    mockFetchMetrics.mockImplementation((agentType: string) => {
      if (agentType === "eligibility" && phase === "initial") {
        return Promise.reject(new Error("Service unavailable"));
      }
      return Promise.resolve(makeMockMetrics(agentType));
    });

    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    );

    // Wait for warning to appear (no automatic retry, so this is fast)
    await waitFor(() => {
      expect(screen.getByTestId("metrics-partial-warning")).toBeInTheDocument();
    });

    // Verify warning text includes the failed agent
    const warning = screen.getByTestId("metrics-partial-warning");
    expect(warning).toHaveTextContent("Eligibility");
    expect(warning).toHaveTextContent("Incomplete chart data");

    // Verify retry button exists
    const retryButton = screen.getByTestId("metrics-retry-button");
    expect(retryButton).toBeInTheDocument();

    // Switch to success phase before clicking retry
    phase = "retry";

    await user.click(retryButton);

    // Warning should be gone after successful retry
    await waitFor(() => {
      expect(screen.queryByTestId("metrics-partial-warning")).not.toBeInTheDocument();
    });
  });

  it("automatically retries failed agent metrics with backoff", async () => {
    // Track how many times eligibility was fetched — auto-retry should
    // re-fetch failed agents without user interaction.
    let eligibilityCalls = 0;

    mockFetchMetrics.mockImplementation((agentType: string) => {
      if (agentType === "eligibility") {
        eligibilityCalls++;
        if (eligibilityCalls <= 1) {
          // First call fails
          return Promise.reject(new Error("Service unavailable"));
        }
      }
      return Promise.resolve(makeMockMetrics(agentType));
    });

    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    );

    // Wait for initial load — warning should appear with auto-retry message
    await waitFor(() => {
      const warning = screen.getByTestId("metrics-partial-warning");
      expect(warning).toBeInTheDocument();
      expect(warning).toHaveTextContent("Retrying automatically");
    });

    // The auto-retry fires after a delay (2s). Wait for it to complete
    // and clear the warning once the retry succeeds.
    await waitFor(
      () => {
        expect(screen.queryByTestId("metrics-partial-warning")).not.toBeInTheDocument();
      },
      { timeout: 5000 },
    );

    // Eligibility should have been fetched at least twice (initial + auto-retry)
    expect(eligibilityCalls).toBeGreaterThanOrEqual(2);
  }, 10000);

  it("shows 'Partial data' badge on chart when some agent metrics fail", async () => {
    mockFetchMetrics.mockImplementation((agentType: string) => {
      if (agentType === "claims") {
        return Promise.reject(new Error("Service unavailable"));
      }
      return Promise.resolve(makeMockMetrics(agentType));
    });

    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText("Dashboard")).toBeInTheDocument();
    });

    // The chart should show a "Partial data" badge
    await waitFor(() => {
      const badge = screen.getByTestId("chart-incomplete-badge");
      expect(badge).toBeInTheDocument();
      expect(badge).toHaveTextContent("Partial data");
    });
  });

  it("does not show metrics warning when all agent metrics load successfully", async () => {
    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText("Dashboard")).toBeInTheDocument();
    });

    // No warning should appear
    expect(screen.queryByTestId("metrics-partial-warning")).not.toBeInTheDocument();
  });
});
