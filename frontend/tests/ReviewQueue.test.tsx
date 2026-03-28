import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import ReviewQueue from "../src/components/reviews/ReviewQueue";
import type { ReviewResponse } from "../src/types";

const mockReviews: ReviewResponse[] = [
  {
    id: "rev-1",
    task_id: "task-001",
    reviewer_id: null,
    status: "pending",
    reason: "Ambiguous coverage response",
    agent_decision: { coverage_active: true },
    confidence_score: 0.55,
    reviewer_notes: null,
    decided_at: null,
    created_at: "2026-03-25T10:00:00Z",
    updated_at: null,
    // First-class fields from API (joined from AgentTask)
    agent_type: "eligibility",
    patient_id: "PAT-0001234567",
  },
  {
    id: "rev-2",
    task_id: "task-002",
    reviewer_id: null,
    status: "pending",
    reason: "Low confidence code suggestion",
    agent_decision: null,
    confidence_score: 0.35,
    reviewer_notes: null,
    decided_at: null,
    created_at: "2026-03-24T08:00:00Z",
    updated_at: null,
    agent_type: "claims",
    patient_id: "PAT-0009876543",
  },
  {
    id: "rev-3",
    task_id: "task-003",
    reviewer_id: "user-1",
    status: "approved",
    reason: "Clinical review needed",
    agent_decision: null,
    confidence_score: 0.72,
    reviewer_notes: "Approved after clinical review",
    decided_at: "2026-03-25T12:00:00Z",
    created_at: "2026-03-25T09:00:00Z",
    updated_at: "2026-03-25T12:00:00Z",
    agent_type: "prior_auth",
    patient_id: null,
  },
];

describe("ReviewQueue", () => {
  it("renders reviews and handles approve click", () => {
    const onSelect = vi.fn();

    render(
      <ReviewQueue
        reviews={mockReviews}
        loading={false}
        onSelectReview={onSelect}
      />,
    );

    expect(screen.getByTestId("review-queue")).toBeInTheDocument();

    // All 3 reviews should render
    expect(screen.getByTestId("review-item-rev-1")).toBeInTheDocument();
    expect(screen.getByTestId("review-item-rev-2")).toBeInTheDocument();
    expect(screen.getByTestId("review-item-rev-3")).toBeInTheDocument();

    // Click first review
    fireEvent.click(screen.getByTestId("review-item-rev-1"));
    expect(onSelect).toHaveBeenCalledWith(mockReviews[0]);
  });

  it("shows empty state when no reviews", () => {
    render(
      <ReviewQueue reviews={[]} loading={false} onSelectReview={vi.fn()} />,
    );

    expect(screen.getByTestId("review-queue-empty")).toBeInTheDocument();
    expect(screen.getByText("No reviews in queue")).toBeInTheDocument();
  });

  it("shows loading state", () => {
    render(
      <ReviewQueue reviews={[]} loading={true} onSelectReview={vi.fn()} />,
    );

    expect(screen.getByTestId("review-queue-loading")).toBeInTheDocument();
  });

  it("displays confidence scores", () => {
    render(
      <ReviewQueue
        reviews={mockReviews}
        loading={false}
        onSelectReview={vi.fn()}
      />,
    );

    expect(screen.getByText("Confidence: 55%")).toBeInTheDocument();
    expect(screen.getByText("Confidence: 35%")).toBeInTheDocument();
  });

  it("displays agent type labels from first-class agent_type field", () => {
    render(
      <ReviewQueue
        reviews={mockReviews}
        loading={false}
        onSelectReview={vi.fn()}
      />,
    );

    expect(screen.getByText("Eligibility")).toBeInTheDocument();
    expect(screen.getByText("Claims & Billing")).toBeInTheDocument();
    expect(screen.getByText("Prior Auth")).toBeInTheDocument();
  });

  it("displays patient info from first-class patient_id field", () => {
    render(
      <ReviewQueue
        reviews={mockReviews}
        loading={false}
        onSelectReview={vi.fn()}
      />,
    );

    // rev-1 has patient_id as first-class field
    expect(screen.getByTestId("review-patient-rev-1")).toBeInTheDocument();
    expect(screen.getByText(/PAT-000123/)).toBeInTheDocument();

    // rev-2 has patient_id as first-class field
    expect(screen.getByTestId("review-patient-rev-2")).toBeInTheDocument();
    expect(screen.getByText(/PAT-000987/)).toBeInTheDocument();
  });

  it("renders agent_type and patient unconditionally when API provides them", () => {
    // Even with null agent_decision, agent_type and patient_id are available
    // because they come from the joined AgentTask, not the JSON blob
    const reviewsWithNullDecision: ReviewResponse[] = [
      {
        id: "rev-4",
        task_id: "task-004",
        reviewer_id: null,
        status: "pending",
        reason: "Test review",
        agent_decision: null,
        confidence_score: 0.5,
        reviewer_notes: null,
        decided_at: null,
        created_at: "2026-03-25T10:00:00Z",
        updated_at: null,
        agent_type: "scheduling",
        patient_id: "PAT-TESTING",
      },
    ];

    render(
      <ReviewQueue
        reviews={reviewsWithNullDecision}
        loading={false}
        onSelectReview={vi.fn()}
      />,
    );

    expect(screen.getByTestId("review-agent-type-rev-4")).toBeInTheDocument();
    expect(screen.getByText("Scheduling")).toBeInTheDocument();
    expect(screen.getByTestId("review-patient-rev-4")).toBeInTheDocument();
    expect(screen.getByText(/PAT-TESTING/)).toBeInTheDocument();
  });
});
