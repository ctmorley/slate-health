import {
  AlertTriangle,
  Clock,
  CheckCircle2,
  XCircle,
  ChevronRight,
} from "lucide-react";
import type { ReviewResponse, AgentType } from "@/types";
import { AGENT_LABELS } from "@/types";

interface ReviewQueueProps {
  reviews: ReviewResponse[];
  loading: boolean;
  onSelectReview: (review: ReviewResponse) => void;
}

function formatTimeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function statusIcon(status: string) {
  switch (status) {
    case "approved":
      return <CheckCircle2 size={14} className="text-green-600" />;
    case "rejected":
      return <XCircle size={14} className="text-red-600" />;
    case "escalated":
      return <AlertTriangle size={14} className="text-orange-600" />;
    default:
      return <Clock size={14} className="text-yellow-600" />;
  }
}

function statusBadgeClass(status: string): string {
  switch (status) {
    case "approved":
      return "bg-green-100 text-green-800";
    case "rejected":
      return "bg-red-100 text-red-800";
    case "escalated":
      return "bg-orange-100 text-orange-800";
    default:
      return "bg-yellow-100 text-yellow-800";
  }
}

/**
 * Resolve the agent type label to display.
 *
 * Prefers the first-class ``agent_type`` field returned by the API
 * (populated via a join to AgentTask).  Falls back to the optional
 * ``agent_decision.agent_type`` for backward compatibility.
 */
function resolveAgentType(review: ReviewResponse): string | null {
  const agentType: AgentType | null =
    review.agent_type ??
    ((review.agent_decision?.agent_type as AgentType | undefined) ?? null);
  if (!agentType) return null;
  return AGENT_LABELS[agentType] ?? agentType;
}

/**
 * Resolve a patient display string.
 *
 * Prefers the first-class ``patient_id`` from the API (joined from
 * AgentTask).  Falls back to agent_decision keys for backward compat.
 */
function resolvePatientDisplay(review: ReviewResponse): string | null {
  if (review.patient_id) return review.patient_id;
  const decision = review.agent_decision;
  if (!decision) return null;
  if (decision.patient_name) return String(decision.patient_name);
  if (decision.patient_id) return String(decision.patient_id);
  return null;
}

export default function ReviewQueue({
  reviews,
  loading,
  onSelectReview,
}: ReviewQueueProps) {
  if (loading) {
    return (
      <div className="flex items-center justify-center py-12" data-testid="review-queue-loading">
        <div className="h-6 w-6 animate-spin rounded-full border-4 border-teal-600 border-t-transparent" />
      </div>
    );
  }

  if (reviews.length === 0) {
    return (
      <div
        className="rounded-lg border border-gray-200 bg-white py-12 text-center text-sm text-gray-400"
        data-testid="review-queue-empty"
      >
        No reviews in queue
      </div>
    );
  }

  return (
    <div
      className="overflow-hidden rounded-lg border border-gray-200 bg-white"
      data-testid="review-queue"
    >
      <ul className="divide-y divide-gray-100">
        {reviews.map((review) => {
          const agentLabel = resolveAgentType(review);
          const patientDisplay = resolvePatientDisplay(review);

          return (
            <li
              key={review.id}
              onClick={() => onSelectReview(review)}
              className="flex cursor-pointer items-center justify-between px-5 py-4 transition-colors hover:bg-gray-50"
              data-testid={`review-item-${review.id}`}
            >
              <div className="flex-1">
                <div className="flex items-center gap-2">
                  <span
                    className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${statusBadgeClass(review.status)}`}
                  >
                    {statusIcon(review.status)}
                    {review.status}
                  </span>
                  {agentLabel && (
                    <span className="text-xs font-medium text-gray-600" data-testid={`review-agent-type-${review.id}`}>
                      {agentLabel}
                    </span>
                  )}
                </div>
                <p className="mt-1 text-sm text-gray-700">
                  {review.reason || "Confidence below threshold"}
                </p>
                <div className="mt-1 flex items-center gap-3 text-xs text-gray-500">
                  <span>Task: {review.task_id.slice(0, 10)}</span>
                  {patientDisplay && (
                    <span data-testid={`review-patient-${review.id}`}>
                      Patient: {patientDisplay.slice(0, 20)}
                    </span>
                  )}
                  {review.confidence_score != null && (
                    <span>
                      Confidence: {(review.confidence_score * 100).toFixed(0)}%
                    </span>
                  )}
                  <span>{formatTimeAgo(review.created_at)}</span>
                </div>
              </div>
              <ChevronRight size={16} className="text-gray-400" />
            </li>
          );
        })}
      </ul>
    </div>
  );
}
