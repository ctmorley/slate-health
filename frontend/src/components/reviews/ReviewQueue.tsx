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
      return <CheckCircle2 size={14} className="text-mint-500" />;
    case "rejected":
      return <XCircle size={14} className="text-coral-500" />;
    case "escalated":
      return <AlertTriangle size={14} className="text-orange-400" />;
    default:
      return <Clock size={14} className="text-yellow-400" />;
  }
}

function statusBadgeClass(status: string): string {
  switch (status) {
    case "approved":
      return "bg-mint-600/10 text-mint-500";
    case "rejected":
      return "bg-coral-600/10 text-coral-500";
    case "escalated":
      return "bg-orange-500/10 text-orange-400";
    default:
      return "bg-yellow-500/10 text-yellow-400";
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
        <div className="h-6 w-6 animate-spin rounded-full border-4 border-accent-700 border-t-transparent" />
      </div>
    );
  }

  if (reviews.length === 0) {
    return (
      <div
        className="glass-card rounded-lg py-12 text-center text-sm text-slate-500"
        data-testid="review-queue-empty"
      >
        No reviews in queue
      </div>
    );
  }

  return (
    <div
      className="glass-card overflow-hidden rounded-lg"
      data-testid="review-queue"
    >
      <ul className="divide-y divide-glass">
        {reviews.map((review) => {
          const agentLabel = resolveAgentType(review);
          const patientDisplay = resolvePatientDisplay(review);

          return (
            <li
              key={review.id}
              onClick={() => onSelectReview(review)}
              className="flex cursor-pointer items-center justify-between px-5 py-4 transition-colors hover:bg-slate_d-700"
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
                    <span className="text-xs font-medium text-slate-400" data-testid={`review-agent-type-${review.id}`}>
                      {agentLabel}
                    </span>
                  )}
                </div>
                <p className="mt-1 text-sm text-slate-300">
                  {review.reason || "Confidence below threshold"}
                </p>
                <div className="mt-1 flex items-center gap-3 text-xs text-slate-500">
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
              <ChevronRight size={16} className="text-slate-500" />
            </li>
          );
        })}
      </ul>
    </div>
  );
}
