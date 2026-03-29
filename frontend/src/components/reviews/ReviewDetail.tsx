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
      <div className="mb-6 glass-card rounded-lg p-5">
        <div className="flex items-center justify-between">
          <h3 className="text-lg font-semibold text-slate-100">
            Review {review.id.slice(0, 10)}
          </h3>
          <span
            className={`inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-medium ${
              review.status === "approved"
                ? "bg-mint-600/10 text-mint-500"
                : review.status === "rejected"
                  ? "bg-coral-600/10 text-coral-500"
                  : review.status === "escalated"
                    ? "bg-orange-500/10 text-orange-400"
                    : "bg-yellow-500/10 text-yellow-400"
            }`}
          >
            {review.status}
          </span>
        </div>

        <div className="mt-4 grid grid-cols-2 gap-4 text-sm sm:grid-cols-3">
          <div>
            <p className="text-xs text-slate-400">Task ID</p>
            <p className="font-mono text-xs text-slate-100">
              {review.task_id}
            </p>
          </div>
          <div>
            <p className="text-xs text-slate-400">Confidence</p>
            <p className="font-medium text-slate-100">
              {review.confidence_score != null
                ? `${(review.confidence_score * 100).toFixed(0)}%`
                : "-"}
            </p>
          </div>
          <div>
            <p className="text-xs text-slate-400">Created</p>
            <p className="text-slate-100">
              {new Date(review.created_at).toLocaleString()}
            </p>
          </div>
        </div>
      </div>

      {/* Reason */}
      <div className="mb-6 glass-card rounded-lg p-5">
        <h4 className="mb-2 text-sm font-semibold text-slate-100">
          Escalation Reason
        </h4>
        <p className="text-sm text-slate-300">
          {review.reason || "Confidence below threshold"}
        </p>
      </div>

      {/* Agent Decision */}
      <div className="mb-6 glass-card rounded-lg p-5">
        <h4 className="mb-2 text-sm font-semibold text-slate-100">
          Agent Decision
        </h4>
        {Object.keys(decisionData).length > 0 ? (
          <pre className="max-h-60 overflow-auto rounded-md bg-slate_d-900 p-3 font-mono text-xs text-slate-300">
            {decisionJson}
          </pre>
        ) : (
          <p className="text-sm text-slate-500">No decision data available</p>
        )}
      </div>

      {/* Evidence */}
      <div className="mb-6 glass-card rounded-lg p-5" data-testid="review-evidence-section">
        <h4 className="mb-2 text-sm font-semibold text-slate-100">
          Evidence
        </h4>
        {(() => {
          const evidence = decisionData.evidence ?? decisionData.clinical_evidence ?? decisionData.supporting_data;
          if (evidence && typeof evidence === "object" && Object.keys(evidence as Record<string, unknown>).length > 0) {
            return (
              <pre className="max-h-60 overflow-auto rounded-md bg-slate_d-900 p-3 font-mono text-xs text-slate-300">
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
                    <span className="min-w-[140px] font-medium text-slate-400">
                      {key.replace(/_/g, " ")}
                    </span>
                    <span className="text-slate-300">
                      {typeof value === "object" ? JSON.stringify(value) : String(value)}
                    </span>
                  </div>
                ))}
              </div>
            );
          }
          return (
            <p className="text-sm text-slate-500">
              No additional evidence available
            </p>
          );
        })()}
      </div>

      {/* Confidence Breakdown */}
      {review.confidence_score != null && (
        <div className="mb-6 glass-card rounded-lg p-5">
          <h4 className="mb-2 text-sm font-semibold text-slate-100">
            Confidence Score
          </h4>
          <div className="flex items-center gap-3">
            <div className="h-3 flex-1 rounded-full bg-slate_d-600">
              <div
                className={`h-3 rounded-full ${
                  review.confidence_score >= 0.7
                    ? "bg-mint-600"
                    : review.confidence_score >= 0.4
                      ? "bg-yellow-500"
                      : "bg-coral-600"
                }`}
                style={{ width: `${review.confidence_score * 100}%` }}
              />
            </div>
            <span className="text-sm font-medium text-slate-300">
              {(review.confidence_score * 100).toFixed(0)}%
            </span>
          </div>
        </div>
      )}

      {/* Reviewer notes from previous decisions */}
      {review.reviewer_notes && (
        <div className="mb-6 glass-card rounded-lg p-5">
          <h4 className="mb-2 text-sm font-semibold text-slate-100">
            Reviewer Notes
          </h4>
          <p className="text-sm text-slate-300">{review.reviewer_notes}</p>
        </div>
      )}

      {/* Success / Error Messages */}
      {successMessage && (
        <div
          className="mb-4 rounded-md border border-mint-600/30 bg-mint-600/10 p-3 text-sm text-mint-500"
          data-testid="review-success"
        >
          {successMessage}
        </div>
      )}
      {actionError && (
        <div
          className="mb-4 rounded-md border border-coral-600/30 bg-coral-600/10 p-3 text-sm text-coral-500"
          data-testid="review-action-error"
        >
          {actionError}
        </div>
      )}

      {/* Action area */}
      {isPending && (
        <div className="glass-card rounded-lg p-5">
          <h4 className="mb-3 text-sm font-semibold text-slate-100">
            Review Action
          </h4>
          <textarea
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Add notes (optional)..."
            rows={3}
            className="dark-input mb-4 w-full"
            data-testid="review-notes-input"
          />
          <div className="flex gap-3">
            <Button
              onClick={() => handleAction("approve")}
              disabled={acting !== null}
              className="bg-mint-600 hover:bg-mint-500"
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
