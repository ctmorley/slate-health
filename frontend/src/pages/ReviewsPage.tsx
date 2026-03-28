import { useState, useEffect, useCallback, useRef } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { ListChecks } from "lucide-react";
import ReviewQueue from "@/components/reviews/ReviewQueue";
import ReviewDetail from "@/components/reviews/ReviewDetail";
import { listReviews, getReview } from "@/api/reviews";
import { useWebSocket } from "@/hooks/useWebSocket";
import type { ReviewResponse, WsMessage } from "@/types";

export default function ReviewsPage() {
  const { reviewId: routeReviewId } = useParams<{ reviewId?: string }>();
  const navigate = useNavigate();
  const [reviews, setReviews] = useState<ReviewResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedReview, setSelectedReview] = useState<ReviewResponse | null>(
    null,
  );
  const [statusFilter, setStatusFilter] = useState("pending");
  const abortRef = useRef<AbortController | null>(null);

  // If opened via /reviews/:reviewId route, load that review
  useEffect(() => {
    if (!routeReviewId) return;
    let cancelled = false;
    getReview(routeReviewId)
      .then((review) => {
        if (!cancelled) setSelectedReview(review);
      })
      .catch(() => {
        // Review not found — stay on list view
        if (!cancelled) navigate("/reviews", { replace: true });
      });
    return () => { cancelled = true; };
  }, [routeReviewId, navigate]);

  const fetchReviews = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const result = await listReviews({
        status_filter: statusFilter || undefined,
        limit: 50,
        offset: 0,
      }, signal);
      if (signal?.aborted) return;
      // Sort by created_at ascending (oldest first) for pending reviews
      const sorted = [...result.items].sort(
        (a, b) =>
          new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
      );
      setReviews(sorted);
    } catch (err) {
      if (signal?.aborted) return;
      setError(err instanceof Error ? err.message : "Failed to load reviews");
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    fetchReviews(controller.signal);
    return () => { controller.abort(); };
  }, [fetchReviews]);

  // Real-time updates: refresh reviews when a new task enters review status
  // or when a review_created event arrives.
  const handleWsMessage = useCallback(
    (msg: WsMessage) => {
      if (msg.event === "pong") return;
      if (
        msg.event === "task_status_changed" &&
        (msg.data.status === "review" || msg.data.status === "completed")
      ) {
        fetchReviews();
      }
      // Also handle generic review events — the backend may emit these
      if ((msg as unknown as Record<string, unknown>).event === "review_created") {
        fetchReviews();
      }
    },
    [fetchReviews],
  );

  useWebSocket({ onMessage: handleWsMessage });

  const handleActionComplete = useCallback(() => {
    setSelectedReview(null);
    if (routeReviewId) navigate("/reviews", { replace: true });
    // Re-fetch with a fresh controller since the old one may have been aborted
    const controller = new AbortController();
    abortRef.current = controller;
    fetchReviews(controller.signal);
  }, [fetchReviews, routeReviewId, navigate]);

  if (selectedReview) {
    return (
      <ReviewDetail
        review={selectedReview}
        onBack={() => {
          setSelectedReview(null);
          if (routeReviewId) navigate("/reviews", { replace: true });
        }}
        onActionComplete={handleActionComplete}
      />
    );
  }

  return (
    <div>
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold text-gray-900">
            <ListChecks size={22} />
            Reviews
          </h1>
          <p className="mt-1 text-sm text-gray-500">
            Human-in-the-loop review queue for agent decisions.
          </p>
        </div>
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-teal-500 focus:outline-none focus:ring-1 focus:ring-teal-500"
          data-testid="review-status-filter"
        >
          <option value="pending">Pending</option>
          <option value="approved">Approved</option>
          <option value="rejected">Rejected</option>
          <option value="escalated">Escalated</option>
          <option value="">All</option>
        </select>
      </div>

      {error && (
        <div className="mb-4 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
          {error}
        </div>
      )}

      <ReviewQueue
        reviews={reviews}
        loading={loading}
        onSelectReview={setSelectedReview}
      />
    </div>
  );
}
