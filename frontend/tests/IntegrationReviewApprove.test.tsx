import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import ReviewsPage from "../src/pages/ReviewsPage";
import type { ReviewList, ReviewResponse } from "../src/types";

// Mock API modules
vi.mock("../src/api/reviews", () => ({
  listReviews: vi.fn(),
  approveReview: vi.fn(),
  rejectReview: vi.fn(),
  escalateReview: vi.fn(),
}));

// Capture the onMessage callback so we can inject WS messages in tests
let capturedOnMessage: ((msg: unknown) => void) | null = null;

vi.mock("../src/hooks/useWebSocket", () => ({
  useWebSocket: (opts?: { onMessage?: (msg: unknown) => void }) => {
    if (opts?.onMessage) {
      capturedOnMessage = opts.onMessage;
    }
    return { isConnected: true, lastMessage: null };
  },
}));

import { listReviews, approveReview } from "../src/api/reviews";
const mockListReviews = vi.mocked(listReviews);
const mockApproveReview = vi.mocked(approveReview);

const pendingReview: ReviewResponse = {
  id: "rev-100",
  task_id: "task-100",
  reviewer_id: null,
  status: "pending",
  reason: "Multiple coverage matches found",
  agent_decision: { coverage_active: true },
  confidence_score: 0.48,
  reviewer_notes: null,
  decided_at: null,
  created_at: "2026-03-24T08:00:00Z",
  updated_at: null,
  agent_type: "eligibility",
  patient_id: "PAT-100",
};

const mockReviewList: ReviewList = {
  items: [pendingReview],
  total: 1,
  limit: 50,
  offset: 0,
};

describe("Integration: approve review via UI flow", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    capturedOnMessage = null;
    mockListReviews.mockResolvedValue(mockReviewList);
  });

  it("selects review from queue -> approves -> verifies API called", async () => {
    const approvedReview = { ...pendingReview, status: "approved" };
    mockApproveReview.mockResolvedValue(approvedReview);

    // After approval, refresh shows empty list
    let callCount = 0;
    mockListReviews.mockImplementation(async () => {
      callCount++;
      if (callCount <= 1) return mockReviewList;
      return { items: [], total: 0, limit: 50, offset: 0 };
    });

    render(
      <MemoryRouter>
        <ReviewsPage />
      </MemoryRouter>,
    );

    // Wait for review to appear
    await waitFor(() => {
      expect(screen.getByTestId("review-item-rev-100")).toBeInTheDocument();
    });

    // Click review to open detail
    fireEvent.click(screen.getByTestId("review-item-rev-100"));

    // Should see detail view with approve button
    await waitFor(() => {
      expect(screen.getByTestId("review-detail")).toBeInTheDocument();
    });
    expect(screen.getByTestId("approve-button")).toBeInTheDocument();

    // Click approve
    fireEvent.click(screen.getByTestId("approve-button"));

    // Verify API was called
    await waitFor(() => {
      expect(mockApproveReview).toHaveBeenCalledWith("rev-100", undefined);
    });

    // Should show success message
    await waitFor(() => {
      expect(screen.getByTestId("review-success")).toBeInTheDocument();
    });

    // After the action completes, the list refreshes with empty results
    // (the approved review is removed from the pending queue)
    await waitFor(() => {
      // The list was refreshed (called more than once)
      expect(mockListReviews.mock.calls.length).toBeGreaterThanOrEqual(2);
    });

    // Verify the re-fetch returns empty list (approved review removed from queue)
    await waitFor(() => {
      const lastCallResult = mockListReviews.mock.results[mockListReviews.mock.results.length - 1];
      if (lastCallResult?.type === "return") {
        return lastCallResult.value.then((val: { items: unknown[] }) => {
          expect(val.items).toHaveLength(0);
        });
      }
    });
  });
});

describe("Realtime: WebSocket triggers review queue refresh", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    capturedOnMessage = null;
    mockListReviews.mockResolvedValue(mockReviewList);
  });

  it("refreshes review queue when task_status_changed with status=review arrives via WS", async () => {
    render(
      <MemoryRouter>
        <ReviewsPage />
      </MemoryRouter>,
    );

    // Wait for initial load
    await waitFor(() => {
      expect(screen.getByTestId("review-item-rev-100")).toBeInTheDocument();
    });

    const initialCallCount = mockListReviews.mock.calls.length;

    // Now add a second review in the next API response
    const newReview: ReviewResponse = {
      id: "rev-200",
      task_id: "task-200",
      reviewer_id: null,
      status: "pending",
      reason: "New escalation from agent",
      agent_decision: null,
      confidence_score: 0.3,
      reviewer_notes: null,
      decided_at: null,
      created_at: "2026-03-25T15:00:00Z",
      updated_at: null,
      agent_type: "claims",
      patient_id: "PAT-200",
    };

    mockListReviews.mockResolvedValue({
      items: [pendingReview, newReview],
      total: 2,
      limit: 50,
      offset: 0,
    });

    // Simulate a WebSocket message arriving — wrap in act() since this
    // triggers state updates in the component via the onMessage callback.
    expect(capturedOnMessage).not.toBeNull();
    act(() => {
      capturedOnMessage!({
        event: "task_status_changed",
        data: { task_id: "task-200", agent_type: "claims", status: "review" },
      });
    });

    // The queue should refresh and show the new review
    await waitFor(() => {
      expect(mockListReviews.mock.calls.length).toBeGreaterThan(initialCallCount);
    });

    // Verify the new review now appears in the DOM
    await waitFor(() => {
      expect(screen.getByTestId("review-item-rev-200")).toBeInTheDocument();
    });
  });
});
