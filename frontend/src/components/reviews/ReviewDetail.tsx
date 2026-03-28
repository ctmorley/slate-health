import { useState } from "react";
import {
  ArrowLeft,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Loader2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { approveReview, rejectReview, escalateReview } from "@/api/reviews";
import type { ReviewResponse } from "@/types";

interface ReviewDetailProps {
  review: ReviewResponse;
  onBack: () => void;
  onActionComplete: () => void;
}

type ActionType = "approve" | "reject" | "escalate";

export default function ReviewDetail({
  review,
  onBack,
  onActionComplete,
}: ReviewDetailProps) {
  const [notes, setNotes] = useState("");
  const [acting, setActing] = useState<ActionType | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);

  const isPending = review.status === "pending";

  async function handleAction(action: ActionType) {
    setActing(action);
    setActionError(null);
    setSuccessMessage(null);
    try {
      const req = notes.trim() ? { notes: notes.trim() } : undefined;
      switch (action) {
        case "approve":
          await approveReview(review.id, req);
          break;
        case "reject":
          await rejectReview(review.id, req);
          break;
        case "escalate":
          await escalateReview(review.id, req);
          break;
      }
      setSuccessMessage(
        `Review ${action === "approve" ? "approved" : action === "reject" ? "rejected" : "escalated"} successfully`,
      );
      // Let parent know to refresh the list
      setTimeout(() => onActionComplete(), 1000);
    } catch (err) {
      setActionError(
        err instanceof Error ? err.message : `Failed to ${action} review`,
      );
    } finally {
      setActing(null);
    }
  }

  const decisionData = review.agent_decision ?? {};
  const decisionJson = JSON.stringify(decisionData, null, 2);

  return (
    <div data-testid="review-detail">
      <Button variant="ghost" size="sm" onClick={onBack} className="mb-4">
        <ArrowLeft size={16} />
        Back to queue
      </Button>

      {/* Header */}
      <div className="mb-6 rounded-lg border border-gray-200 bg-white p-5">
        <div className="flex items-center justify-between">
          <h3 className="text-lg font-semibold text-gray-900">
            Review {review.id.slice(0, 10)}
          </h3>
          <span
            className={`inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-medium ${
              review.status === "approved"
                ? "bg-green-100 text-green-800"
                : review.status === "rejected"
                  ? "bg-red-100 text-red-800"
                  : review.status === "escalated"
                    ? "bg-orange-100 text-orange-800"
                    : "bg-yellow-100 text-yellow-800"
            }`}
          >
            {review.status}
          </span>
        </div>

        <div className="mt-4 grid grid-cols-2 gap-4 text-sm sm:grid-cols-3">
          <div>
            <p className="text-xs text-gray-500">Task ID</p>
            <p className="font-mono text-xs text-gray-900">
              {review.task_id}
            </p>
          </div>
          <div>
            <p className="text-xs text-gray-500">Confidence</p>
            <p className="font-medium text-gray-900">
              {review.confidence_score != null
                ? `${(review.confidence_score * 100).toFixed(0)}%`
                : "-"}
            </p>
          </div>
          <div>
            <p className="text-xs text-gray-500">Created</p>
            <p className="text-gray-900">
              {new Date(review.created_at).toLocaleString()}
            </p>
          </div>
        </div>
      </div>

      {/* Reason */}
      <div className="mb-6 rounded-lg border border-gray-200 bg-white p-5">
        <h4 className="mb-2 text-sm font-semibold text-gray-900">
          Escalation Reason
        </h4>
        <p className="text-sm text-gray-700">
          {review.reason || "Confidence below threshold"}
        </p>
      </div>

      {/* Agent Decision */}
      <div className="mb-6 rounded-lg border border-gray-200 bg-white p-5">
        <h4 className="mb-2 text-sm font-semibold text-gray-900">
          Agent Decision
        </h4>
        {Object.keys(decisionData).length > 0 ? (
          <pre className="max-h-60 overflow-auto rounded-md bg-gray-50 p-3 text-xs text-gray-700">
            {decisionJson}
          </pre>
        ) : (
          <p className="text-sm text-gray-400">No decision data available</p>
        )}
      </div>

      {/* Evidence */}
      <div className="mb-6 rounded-lg border border-gray-200 bg-white p-5" data-testid="review-evidence-section">
        <h4 className="mb-2 text-sm font-semibold text-gray-900">
          Evidence
        </h4>
        {(() => {
          const evidence = decisionData.evidence ?? decisionData.clinical_evidence ?? decisionData.supporting_data;
          if (evidence && typeof evidence === "object" && Object.keys(evidence as Record<string, unknown>).length > 0) {
            return (
              <pre className="max-h-60 overflow-auto rounded-md bg-gray-50 p-3 text-xs text-gray-700">
                {JSON.stringify(evidence, null, 2)}
              </pre>
            );
          }
          // Fallback: extract evidence-like fields from decision data
          const evidenceFields: Record<string, unknown> = {};
          for (const [key, value] of Object.entries(decisionData)) {
            if (
              key !== "agent_type" &&
              key !== "confidence" &&
              key !== "needs_review" &&
              key !== "review_reason" &&
              key !== "status" &&
              value != null
            ) {
              evidenceFields[key] = value;
            }
          }
          if (Object.keys(evidenceFields).length > 0) {
            return (
              <div className="space-y-2">
                {Object.entries(evidenceFields).map(([key, value]) => (
                  <div key={key} className="flex gap-2 text-sm">
                    <span className="min-w-[140px] font-medium text-gray-500">
                      {key.replace(/_/g, " ")}
                    </span>
                    <span className="text-gray-700">
                      {typeof value === "object" ? JSON.stringify(value) : String(value)}
                    </span>
                  </div>
                ))}
              </div>
            );
          }
          return (
            <p className="text-sm text-gray-400">
              No additional evidence available
            </p>
          );
        })()}
      </div>

      {/* Confidence Breakdown */}
      {review.confidence_score != null && (
        <div className="mb-6 rounded-lg border border-gray-200 bg-white p-5">
          <h4 className="mb-2 text-sm font-semibold text-gray-900">
            Confidence Score
          </h4>
          <div className="flex items-center gap-3">
            <div className="h-3 flex-1 rounded-full bg-gray-200">
              <div
                className={`h-3 rounded-full ${
                  review.confidence_score >= 0.7
                    ? "bg-green-500"
                    : review.confidence_score >= 0.4
                      ? "bg-yellow-500"
                      : "bg-red-500"
                }`}
                style={{ width: `${review.confidence_score * 100}%` }}
              />
            </div>
            <span className="text-sm font-medium text-gray-700">
              {(review.confidence_score * 100).toFixed(0)}%
            </span>
          </div>
        </div>
      )}

      {/* Reviewer notes from previous decisions */}
      {review.reviewer_notes && (
        <div className="mb-6 rounded-lg border border-gray-200 bg-white p-5">
          <h4 className="mb-2 text-sm font-semibold text-gray-900">
            Reviewer Notes
          </h4>
          <p className="text-sm text-gray-700">{review.reviewer_notes}</p>
        </div>
      )}

      {/* Success / Error Messages */}
      {successMessage && (
        <div
          className="mb-4 rounded-md border border-green-200 bg-green-50 p-3 text-sm text-green-700"
          data-testid="review-success"
        >
          {successMessage}
        </div>
      )}
      {actionError && (
        <div
          className="mb-4 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700"
          data-testid="review-action-error"
        >
          {actionError}
        </div>
      )}

      {/* Action area */}
      {isPending && (
        <div className="rounded-lg border border-gray-200 bg-white p-5">
          <h4 className="mb-3 text-sm font-semibold text-gray-900">
            Review Action
          </h4>
          <textarea
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Add notes (optional)..."
            rows={3}
            className="mb-4 w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-teal-500 focus:outline-none focus:ring-1 focus:ring-teal-500"
            data-testid="review-notes-input"
          />
          <div className="flex gap-3">
            <Button
              onClick={() => handleAction("approve")}
              disabled={acting !== null}
              className="bg-green-600 hover:bg-green-700"
              data-testid="approve-button"
            >
              {acting === "approve" ? (
                <Loader2 size={16} className="animate-spin" />
              ) : (
                <CheckCircle2 size={16} />
              )}
              Approve
            </Button>
            <Button
              variant="destructive"
              onClick={() => handleAction("reject")}
              disabled={acting !== null}
              data-testid="reject-button"
            >
              {acting === "reject" ? (
                <Loader2 size={16} className="animate-spin" />
              ) : (
                <XCircle size={16} />
              )}
              Reject
            </Button>
            <Button
              variant="outline"
              onClick={() => handleAction("escalate")}
              disabled={acting !== null}
              data-testid="escalate-button"
            >
              {acting === "escalate" ? (
                <Loader2 size={16} className="animate-spin" />
              ) : (
                <AlertTriangle size={16} />
              )}
              Escalate
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
