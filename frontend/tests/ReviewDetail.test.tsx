import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import ReviewDetail from "../src/components/reviews/ReviewDetail";
import type { ReviewResponse } from "../src/types";

vi.mock("../src/api/reviews", () => ({
  approveReview: vi.fn(),
  rejectReview: vi.fn(),
  escalateReview: vi.fn(),
}));

import { approveReview, rejectReview, escalateReview } from "../src/api/reviews";
const mockApprove = vi.mocked(approveReview);
const mockReject = vi.mocked(rejectReview);
const mockEscalate = vi.mocked(escalateReview);

const pendingReview: ReviewResponse = {
  id: "rev-1",
  task_id: "task-001",
  reviewer_id: null,
  status: "pending",
  reason: "Ambiguous coverage response",
  agent_decision: { coverage_active: true, plan: "Gold PPO" },
  confidence_score: 0.55,
  reviewer_notes: null,
  decided_at: null,
  created_at: "2026-03-25T10:00:00Z",
  updated_at: null,
};

const approvedReview: ReviewResponse = {
  ...pendingReview,
  status: "approved",
  reviewer_notes: "Looks good",
};

describe("ReviewDetail", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows decision details and approve/reject/escalate buttons", () => {
    render(
      <MemoryRouter>
        <ReviewDetail
          review={pendingReview}
          onBack={vi.fn()}
          onActionComplete={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(screen.getByTestId("review-detail")).toBeInTheDocument();
    expect(screen.getByText("Ambiguous coverage response")).toBeInTheDocument();
    // "55%" appears in both the header stats and the confidence bar
    expect(screen.getAllByText("55%").length).toBeGreaterThan(0);
    expect(screen.getByTestId("approve-button")).toBeInTheDocument();
    expect(screen.getByTestId("reject-button")).toBeInTheDocument();
    expect(screen.getByTestId("escalate-button")).toBeInTheDocument();
  });

  it("hides action buttons for non-pending reviews", () => {
    render(
      <MemoryRouter>
        <ReviewDetail
          review={approvedReview}
          onBack={vi.fn()}
          onActionComplete={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(screen.queryByTestId("approve-button")).not.toBeInTheDocument();
    expect(screen.queryByTestId("reject-button")).not.toBeInTheDocument();
  });

  it("calls approveReview API on approve click", async () => {
    mockApprove.mockResolvedValue(approvedReview);
    const onActionComplete = vi.fn();

    render(
      <MemoryRouter>
        <ReviewDetail
          review={pendingReview}
          onBack={vi.fn()}
          onActionComplete={onActionComplete}
        />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByTestId("approve-button"));

    await waitFor(() => {
      expect(mockApprove).toHaveBeenCalledWith("rev-1", undefined);
    });

    await waitFor(() => {
      expect(screen.getByTestId("review-success")).toBeInTheDocument();
    });
  });

  it("calls rejectReview API on reject click with notes", async () => {
    mockReject.mockResolvedValue({ ...pendingReview, status: "rejected" });

    render(
      <MemoryRouter>
        <ReviewDetail
          review={pendingReview}
          onBack={vi.fn()}
          onActionComplete={vi.fn()}
        />
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByTestId("review-notes-input"), {
      target: { value: "Incorrect coding" },
    });
    fireEvent.click(screen.getByTestId("reject-button"));

    await waitFor(() => {
      expect(mockReject).toHaveBeenCalledWith("rev-1", {
        notes: "Incorrect coding",
      });
    });
  });

  it("shows agent decision JSON", () => {
    render(
      <MemoryRouter>
        <ReviewDetail
          review={pendingReview}
          onBack={vi.fn()}
          onActionComplete={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText(/"coverage_active": true/)).toBeInTheDocument();
  });
});
