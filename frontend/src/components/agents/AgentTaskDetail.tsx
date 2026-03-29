import { useState, useEffect } from "react";
import { Link } from "react-router-dom";
import {
  ArrowLeft,
  Clock,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Loader2,
  Ban,
  ExternalLink,
  Shield,
  ListChecks,
  FileText,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { getTask } from "@/api/agents";
import { listReviews } from "@/api/reviews";
import { listAuditLogs } from "@/api/audit";
import type { AgentType, AgentTaskResponse, TaskStatus, ReviewResponse, AuditLogEntry } from "@/types";

interface AgentTaskDetailProps {
  agentType: AgentType;
  taskId: string;
  onBack: () => void;
}

const STATUS_ICON: Record<TaskStatus, React.ReactNode> = {
  pending: <Clock size={16} className="text-yellow-400" />,
  running: <Loader2 size={16} className="animate-spin text-blue-400" />,
  completed: <CheckCircle2 size={16} className="text-mint-500" />,
  failed: <XCircle size={16} className="text-coral-500" />,
  review: <AlertTriangle size={16} className="text-orange-400" />,
  cancelled: <Ban size={16} className="text-slate-500" />,
};

const STATUS_LABEL: Record<TaskStatus, string> = {
  pending: "Pending",
  running: "Running",
  completed: "Completed",
  failed: "Failed",
  review: "In Review",
  cancelled: "Cancelled",
};

function JsonBlock({ data, label }: { data: unknown; label: string }) {
  const [expanded, setExpanded] = useState(false);
  if (data == null) return null;

  const json = JSON.stringify(data, null, 2);
  const preview = json.length > 200 && !expanded ? json.slice(0, 200) + "..." : json;

  return (
    <div data-testid={`json-block-${label}`}>
      <h4 className="mb-1 text-xs font-semibold uppercase tracking-wider text-slate-400">
        {label}
      </h4>
      <pre className="max-h-80 overflow-auto rounded-md bg-slate_d-900 p-3 font-mono text-xs text-slate-300">
        {preview}
      </pre>
      {json.length > 200 && (
        <button
          onClick={() => setExpanded(!expanded)}
          className="mt-1 text-xs text-accent-600 hover:underline"
        >
          {expanded ? "Show less" : "Show more"}
        </button>
      )}
    </div>
  );
}

function StatusTimeline({ task }: { task: AgentTaskResponse }) {
  const events: { label: string; time: string | null; active: boolean }[] = [
    { label: "Created", time: task.created_at, active: true },
    {
      label: "Running",
      time: task.status !== "pending" ? task.updated_at : null,
      active: ["running", "completed", "failed", "review", "cancelled"].includes(
        task.status,
      ),
    },
    {
      label: task.status === "failed"
        ? "Failed"
        : task.status === "review"
          ? "In Review"
          : task.status === "cancelled"
            ? "Cancelled"
            : "Completed",
      time: ["completed", "failed", "review", "cancelled"].includes(task.status)
        ? task.updated_at
        : null,
      active: ["completed", "failed", "review", "cancelled"].includes(
        task.status,
      ),
    },
  ];

  return (
    <div data-testid="status-timeline">
      <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-400">
        Timeline
      </h4>
      <div className="space-y-3">
        {events.map((event, idx) => (
          <div key={idx} className="flex items-start gap-3">
            <div
              className={`mt-0.5 h-3 w-3 rounded-full border-2 ${
                event.active
                  ? "border-accent-700 bg-accent-700"
                  : "border-slate-600 bg-slate_d-800"
              }`}
            />
            <div>
              <p
                className={`text-sm font-medium ${
                  event.active ? "text-slate-100" : "text-slate-500"
                }`}
              >
                {event.label}
              </p>
              {event.time && (
                <p className="text-xs text-slate-400">
                  {new Date(event.time).toLocaleString()}
                </p>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function LinkedReviewSection({ review }: { review: ReviewResponse }) {
  return (
    <div data-testid="linked-review-section" className="glass-card rounded-lg p-4">
      <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-slate-400">
        <ListChecks size={14} />
        Linked Review
      </h4>
      <div className="space-y-2 text-sm">
        <div className="flex justify-between">
          <span className="text-slate-400">Status</span>
          <span className="font-medium text-slate-100">{review.status}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-slate-400">Reason</span>
          <span className="font-medium text-slate-100">{review.reason || "-"}</span>
        </div>
        {review.confidence_score != null && (
          <div className="flex justify-between">
            <span className="text-slate-400">Confidence</span>
            <span className="font-medium text-slate-100">
              {(review.confidence_score * 100).toFixed(0)}%
            </span>
          </div>
        )}
        {review.reviewer_notes && (
          <div className="flex justify-between">
            <span className="text-slate-400">Notes</span>
            <span className="font-medium text-slate-100">{review.reviewer_notes}</span>
          </div>
        )}
        {review.decided_at && (
          <div className="flex justify-between">
            <span className="text-slate-400">Decided</span>
            <span className="font-medium text-slate-100">
              {new Date(review.decided_at).toLocaleString()}
            </span>
          </div>
        )}
      </div>
      <Link
        to={`/reviews/${review.id}`}
        className="mt-3 inline-flex items-center gap-1 text-sm font-medium text-accent-600 hover:underline"
        data-testid="linked-review-link"
      >
        Open Review Detail
        <ExternalLink size={12} />
      </Link>
    </div>
  );
}

function AuditEntriesSection({ entries }: { entries: AuditLogEntry[] }) {
  return (
    <div data-testid="audit-entries-section" className="glass-card rounded-lg p-4">
      <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-slate-400">
        <FileText size={14} />
        Audit Trail
      </h4>
      {entries.length === 0 ? (
        <p className="text-sm text-slate-500" data-testid="audit-entries-empty">No audit entries</p>
      ) : (
        <div className="space-y-2">
          {entries.map((entry) => (
            <div key={entry.id} className="flex items-start gap-2 text-xs" data-testid={`audit-entry-${entry.id}`}>
              <div className="mt-0.5">
                {entry.phi_accessed ? (
                  <Shield size={12} className="text-amber-500" />
                ) : (
                  <div className="h-3 w-3 rounded-full bg-slate_d-600" />
                )}
              </div>
              <div className="flex-1">
                <span className="font-medium text-slate-300">{entry.action ?? "-"}</span>
                <span className="ml-2 text-slate-500">
                  {entry.timestamp ? new Date(entry.timestamp).toLocaleString() : "-"}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function AgentTaskDetail({
  agentType,
  taskId,
  onBack,
}: AgentTaskDetailProps) {
  const [task, setTask] = useState<AgentTaskResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [linkedReview, setLinkedReview] = useState<ReviewResponse | null>(null);
  const [auditEntries, setAuditEntries] = useState<AuditLogEntry[]>([]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getTask(agentType, taskId)
      .then((data) => {
        if (!cancelled) setTask(data);
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof Error ? err.message : "Failed to load task");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [agentType, taskId]);

  // Fetch linked review for this task using server-side task_id filter
  useEffect(() => {
    if (!task) return;
    let cancelled = false;
    listReviews({ task_id: task.id, limit: 1, offset: 0 })
      .then((result) => {
        if (cancelled) return;
        setLinkedReview(result.items.length > 0 ? result.items[0] : null);
      })
      .catch(() => {
        // Non-critical -- just don't show the section
        if (!cancelled) setLinkedReview(null);
      });

    return () => { cancelled = true; };
  }, [task]);

  // Fetch audit entries for this task using server-side resource_id filter
  useEffect(() => {
    if (!task) return;
    let cancelled = false;
    listAuditLogs({ resource_type: "agent_task", resource_id: task.id, limit: 100, offset: 0 })
      .then((result) => {
        if (cancelled) return;
        setAuditEntries(result.items);
      })
      .catch(() => {
        if (!cancelled) setAuditEntries([]);
      });
    return () => { cancelled = true; };
  }, [task]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12" data-testid="task-detail-loading">
        <div className="h-6 w-6 animate-spin rounded-full border-4 border-accent-700 border-t-transparent" />
      </div>
    );
  }

  if (error || !task) {
    return (
      <div data-testid="task-detail-error">
        <Button variant="ghost" size="sm" onClick={onBack} className="mb-4">
          <ArrowLeft size={16} />
          Back to list
        </Button>
        <div className="glass-card rounded-lg p-4 text-sm text-coral-500">
          {error ?? "Task not found"}
        </div>
      </div>
    );
  }

  return (
    <div data-testid="agent-task-detail">
      <Button variant="ghost" size="sm" onClick={onBack} className="mb-4">
        <ArrowLeft size={16} />
        Back to list
      </Button>

      {/* Header */}
      <div className="mb-6 glass-card rounded-lg p-5">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-lg font-semibold text-slate-100">
              Task {task.task_id.slice(0, 12)}
            </h3>
            <p className="mt-1 font-mono text-xs text-slate-400">
              Full ID: {task.task_id}
            </p>
          </div>
          <div className="flex items-center gap-2">
            {STATUS_ICON[task.status]}
            <span className="text-sm font-medium text-slate-300">
              {STATUS_LABEL[task.status] ?? task.status}
            </span>
          </div>
        </div>

        <div className="mt-4 grid grid-cols-2 gap-4 text-sm sm:grid-cols-4">
          <div>
            <p className="text-xs text-slate-400">Agent Type</p>
            <p className="font-medium text-slate-100">{task.agent_type}</p>
          </div>
          <div>
            <p className="text-xs text-slate-400">Patient ID</p>
            <p className="font-medium text-slate-100">
              {task.patient_id ?? "-"}
            </p>
          </div>
          <div>
            <p className="text-xs text-slate-400">Confidence</p>
            <p className="font-medium text-slate-100">
              {task.confidence_score != null
                ? `${(task.confidence_score * 100).toFixed(0)}%`
                : "-"}
            </p>
          </div>
          <div>
            <p className="text-xs text-slate-400">Workflow</p>
            {task.workflow_execution_id ? (
              <Link
                to={`/workflows/${task.workflow_execution_id}`}
                className="inline-flex items-center gap-1 font-medium text-accent-600 hover:underline"
                data-testid="workflow-detail-link"
              >
                {task.workflow_execution_id.slice(0, 8)}
                <ExternalLink size={12} />
              </Link>
            ) : (
              <p className="font-medium text-slate-100">-</p>
            )}
          </div>
        </div>
      </div>

      {/* Content grid */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        {/* Left column: Input/Output */}
        <div className="space-y-4 lg:col-span-2">
          {task.error_message && (
            <div className="rounded-lg border border-coral-600/30 bg-coral-600/10 p-4">
              <h4 className="mb-1 text-xs font-semibold uppercase tracking-wider text-coral-500">
                Error
              </h4>
              <p className="text-sm text-coral-400">{task.error_message}</p>
            </div>
          )}

          <div className="glass-card rounded-lg p-4">
            <JsonBlock data={task.input_data} label="Input Data" />
          </div>

          <div className="glass-card rounded-lg p-4">
            <JsonBlock data={task.output_data} label="Output Data" />
          </div>

          {/* Audit entries section */}
          <AuditEntriesSection entries={auditEntries} />
        </div>

        {/* Right column: Timeline + Linked Review */}
        <div className="space-y-4">
          <div className="glass-card rounded-lg p-4">
            <StatusTimeline task={task} />
          </div>

          {/* Linked review */}
          {linkedReview && <LinkedReviewSection review={linkedReview} />}
        </div>
      </div>
    </div>
  );
}
