import { useState, useEffect } from "react";
import {
  ArrowLeft,
  CheckCircle2,
  XCircle,
  Clock,
  Loader2,
  ChevronDown,
  ChevronUp,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { getWorkflow, getWorkflowHistory } from "@/api/workflows";
import type {
  WorkflowExecutionResponse,
  WorkflowHistoryEvent,
  AgentType,
} from "@/types";
import { AGENT_LABELS } from "@/types";

interface WorkflowDetailProps {
  workflowId: string;
  onBack: () => void;
}

function eventIcon(eventType: string) {
  if (eventType.includes("Completed") || eventType.includes("completed"))
    return <CheckCircle2 size={14} className="text-mint-500" />;
  if (eventType.includes("Failed") || eventType.includes("failed"))
    return <XCircle size={14} className="text-coral-500" />;
  if (eventType.includes("Started") || eventType.includes("started"))
    return <Loader2 size={14} className="text-blue-400" />;
  return <Clock size={14} className="text-slate-500" />;
}

function EventRow({ event }: { event: WorkflowHistoryEvent }) {
  const [expanded, setExpanded] = useState(false);
  const hasDetails =
    event.details && Object.keys(event.details).length > 0;

  return (
    <li
      className="border-l-2 border-slate_d-600 pb-4 pl-4"
      data-testid={`event-${event.event_id}`}
    >
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-2">
          <div className="-ml-[21px] rounded-full bg-slate_d-800 p-0.5">
            {eventIcon(event.event_type)}
          </div>
          <div>
            <p className="text-sm font-medium text-slate-100">
              {event.event_type}
            </p>
            <p className="text-xs text-slate-400">
              {new Date(event.timestamp).toLocaleString()}
            </p>
          </div>
        </div>
        {hasDetails && (
          <button
            onClick={() => setExpanded(!expanded)}
            className="flex items-center gap-1 text-xs text-accent-600 hover:underline"
            data-testid={`toggle-event-${event.event_id}`}
          >
            {expanded ? (
              <>
                Hide <ChevronUp size={12} />
              </>
            ) : (
              <>
                Details <ChevronDown size={12} />
              </>
            )}
          </button>
        )}
      </div>
      {expanded && hasDetails && (
        <pre className="mt-2 max-h-40 overflow-auto rounded-md bg-slate_d-900 p-2 font-mono text-xs text-slate-300">
          {JSON.stringify(event.details, null, 2)}
        </pre>
      )}
    </li>
  );
}

export default function WorkflowDetail({
  workflowId,
  onBack,
}: WorkflowDetailProps) {
  const [workflow, setWorkflow] = useState<WorkflowExecutionResponse | null>(
    null,
  );
  const [events, setEvents] = useState<WorkflowHistoryEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    Promise.all([getWorkflow(workflowId), getWorkflowHistory(workflowId)])
      .then(([wfData, histData]) => {
        if (cancelled) return;
        setWorkflow(wfData);
        setEvents(histData.events ?? []);
      })
      .catch((err) => {
        if (!cancelled)
          setError(
            err instanceof Error ? err.message : "Failed to load workflow",
          );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [workflowId]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12" data-testid="workflow-detail-loading">
        <div className="h-6 w-6 animate-spin rounded-full border-4 border-accent-700 border-t-transparent" />
      </div>
    );
  }

  if (error || !workflow) {
    return (
      <div data-testid="workflow-detail-error">
        <Button variant="ghost" size="sm" onClick={onBack} className="mb-4">
          <ArrowLeft size={16} />
          Back to list
        </Button>
        <div className="glass-card rounded-lg p-4 text-sm text-coral-500">
          {error ?? "Workflow not found"}
        </div>
      </div>
    );
  }

  return (
    <div data-testid="workflow-detail">
      <Button variant="ghost" size="sm" onClick={onBack} className="mb-4">
        <ArrowLeft size={16} />
        Back to list
      </Button>

      {/* Header */}
      <div className="mb-6 glass-card rounded-lg p-5">
        <h3 className="text-lg font-semibold text-slate-100">
          Workflow {workflow.workflow_id.slice(0, 16)}
        </h3>
        <p className="mt-1 font-mono text-xs text-slate-400">
          {workflow.workflow_id}
        </p>

        <div className="mt-4 grid grid-cols-2 gap-4 text-sm sm:grid-cols-4">
          <div>
            <p className="text-xs text-slate-400">Agent</p>
            <p className="font-medium text-slate-100">
              {AGENT_LABELS[workflow.agent_type as AgentType] ??
                workflow.agent_type}
            </p>
          </div>
          <div>
            <p className="text-xs text-slate-400">Status</p>
            <p className="font-medium text-slate-100">{workflow.status}</p>
          </div>
          <div>
            <p className="text-xs text-slate-400">Run ID</p>
            <p className="font-mono text-xs text-slate-100">
              {workflow.run_id?.slice(0, 12) ?? "-"}
            </p>
          </div>
          <div>
            <p className="text-xs text-slate-400">Task Queue</p>
            <p className="text-slate-100">{workflow.task_queue ?? "-"}</p>
          </div>
        </div>
      </div>

      {/* Error */}
      {workflow.error_message && (
        <div className="mb-6 rounded-lg border border-coral-600/30 bg-coral-600/10 p-4">
          <h4 className="mb-1 text-xs font-semibold uppercase text-coral-500">
            Error
          </h4>
          <p className="text-sm text-coral-400">{workflow.error_message}</p>
        </div>
      )}

      {/* Event History Timeline */}
      <div className="glass-card rounded-lg p-5">
        <h4 className="mb-4 text-sm font-semibold text-slate-100">
          Event History
        </h4>
        {events.length === 0 ? (
          <p
            className="py-6 text-center text-sm text-slate-500"
            data-testid="no-events"
          >
            No events recorded
          </p>
        ) : (
          <ul className="space-y-0" data-testid="event-timeline">
            {events.map((event) => (
              <EventRow key={event.event_id} event={event} />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
