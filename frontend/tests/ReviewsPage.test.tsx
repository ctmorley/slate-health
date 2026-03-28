import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import ReviewsPage from "../src/pages/ReviewsPage";
import type { ReviewResponse } from "../src/types";

vi.mock("../src/api/reviews", () => ({
  listReviews: vi.fn(),
  getReview: vi.fn(),
  approveReview: vi.fn(),
  rejectReview: vi.fn(),
  escalateReview: vi.fn(),
}));

vi.mock("../src/hooks/useWebSocket", () => ({
  useWebSocket: vi.fn(),
}));

import { listReviews } from "../src/api/reviews";
const mockListReviews = vi.mocked(listReviews);

function makeReview(id: string, createdAt: string): ReviewResponse {
  return {
    id,
    task_id: `task-${id}`,
    reviewer_id: null,
    status: "pending",
    reason: "Ambiguous result",
    agent_decision: {},
    confidence_score: 0.5,
    reviewer_notes: null,
    decided_at: null,
    created_at: createdAt,
    updated_at: null,
  };
}

describe("ReviewsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("sorts pending reviews oldest-first (ascending by created_at)", async () => {
    // Return reviews in non-sorted order (newest first from backend)
    const reviews = [
      makeReview("rev-3", "2026-03-27T10:00:00Z"), // newest
      makeReview("rev-1", "2026-03-25T08:00:00Z"), // oldest
      makeReview("rev-2", "2026-03-26T09:00:00Z"), // middle
    ];
    mockListReviews.mockResolvedValue({
      items: reviews,
      total: 3,
      limit: 50,
      offset: 0,
    });

    render(
      <MemoryRouter>
        <ReviewsPage />
      </MemoryRouter>,
    );

    // Wait for items to render
    await waitFor(() => {
      expect(screen.getByText(/rev-1/)).toBeInTheDocument();
    });

    // The rendered DOM order should be oldest first: rev-1, rev-2, rev-3.
    // We verify by checking the order of the data-testid attributes in the DOM.
    const allRows = screen
      .getAllByTestId(/^review-item-/)
      .map((el) => el.getAttribute("data-testid"));

    expect(allRows).toEqual([
      "review-item-rev-1",
      "review-item-rev-2",
      "review-item-rev-3",
    ]);
  });

  it("renders empty state when no reviews", async () => {
    mockListReviews.mockResolvedValue({
      items: [],
      total: 0,
      limit: 50,
      offset: 0,
    });

    render(
      <MemoryRouter>
        <ReviewsPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("review-queue-empty")).toBeInTheDocument();
    });
  });
});
