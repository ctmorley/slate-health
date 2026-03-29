import {
  Clock,
  CheckCircle2,
  XCircle,
  Loader2,
  Play,
  ChevronRight,
} from "lucide-react";
import type { WorkflowExecutionResponse, AgentType } from "@/types";
import { AGENT_LABELS } from "@/types";

interface WorkflowListProps {
  workflows: WorkflowExecutionResponse[];
  loading: boolean;
  onSelectWorkflow: (workflow: WorkflowExecutionResponse) => void;
}

function statusIcon(status: string) {
  switch (status) {
    case "completed":
      return <CheckCircle2 size={14} className="text-mint-500" />;
    case "failed":
    case "terminated":
      return <XCircle size={14} className="text-coral-500" />;
    case "running":
      return <Loader2 size={14} className="animate-spin text-blue-400" />;
    case "cancelled":
    case "canceled":
      return <XCircle size={14} className="text-slate-500" />;
    default:
      return <Clock size={14} className="text-yellow-400" />;
  }
}

function statusBadgeClass(status: string): string {
  switch (status) {
    case "completed":
      return "bg-mint-600/10 text-mint-500";
    case "failed":
    case "terminated":
      return "bg-coral-600/10 text-coral-500";
    case "running":
      return "bg-blue-500/10 text-blue-400";
    case "cancelled":
    case "canceled":
      return "bg-slate-500/10 text-slate-400";
    default:
      return "bg-yellow-500/10 text-yellow-400";
  }
}

function formatDuration(created: string, updated: string | null): string {
  const start = new Date(created).getTime();
  const end = updated ? new Date(updated).getTime() : Date.now();
  const diffMs = end - start;
  const seconds = Math.floor(diffMs / 1000);
  if (seconds < 60) return `${seconds}s`;
  const mins = Math.floor(seconds / 60);
  if (mins < 60) return `${mins}m ${seconds % 60}s`;
  const hours = Math.floor(mins / 60);
  return `${hours}h ${mins % 60}m`;
}

export default function WorkflowList({
  workflows,
  loading,
  onSelectWorkflow,
}: WorkflowListProps) {
  if (loading) {
    return (
      <div className="flex items-center justify-center py-12" data-testid="workflow-list-loading">
        <div className="h-6 w-6 animate-spin rounded-full border-4 border-accent-700 border-t-transparent" />
      </div>
    );
  }

  if (workflows.length === 0) {
    return (
      <div
        className="glass-card rounded-lg py-12 text-center text-sm text-slate-500"
        data-testid="workflow-list-empty"
      >
        No workflow executions found
      </div>
    );
  }

  return (
    <div
      className="glass-card overflow-hidden rounded-lg"
      data-testid="workflow-list"
    >
      <table className="w-full text-left text-sm">
        <thead className="border-b border-glass bg-slate_d-800">
          <tr>
            <th className="px-4 py-3 font-medium text-slate-400">
              Workflow ID
            </th>
            <th className="px-4 py-3 font-medium text-slate-400">Agent</th>
            <th className="px-4 py-3 font-medium text-slate-400">Status</th>
            <th className="hidden px-4 py-3 font-medium text-slate-400 md:table-cell">
              Duration
            </th>
            <th className="hidden px-4 py-3 font-medium text-slate-400 lg:table-cell">
              Started
            </th>
            <th className="w-10 px-4 py-3" />
          </tr>
        </thead>
        <tbody className="divide-y divide-glass">
          {workflows.map((wf) => (
            <tr
              key={wf.id}
              onClick={() => onSelectWorkflow(wf)}
              className="cursor-pointer transition-colors hover:bg-slate_d-700"
              data-testid={`workflow-row-${wf.id}`}
            >
              <td className="px-4 py-3">
                <div className="flex items-center gap-2">
                  <Play size={14} className="text-slate-500" />
                  <span className="font-mono text-xs text-slate-300">
                    {wf.workflow_id.slice(0, 16)}
                  </span>
                </div>
              </td>
              <td className="px-4 py-3 text-slate-300">
                {AGENT_LABELS[wf.agent_type as AgentType] ?? wf.agent_type}
              </td>
              <td className="px-4 py-3">
                <span
                  className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${statusBadgeClass(wf.status)}`}
                  data-testid={`wf-status-${wf.status}`}
                >
                  {statusIcon(wf.status)}
                  {wf.status}
                </span>
              </td>
              <td className="hidden px-4 py-3 text-xs text-slate-400 md:table-cell">
                {formatDuration(wf.created_at, wf.updated_at)}
              </td>
              <td className="hidden px-4 py-3 text-xs text-slate-400 lg:table-cell">
                {new Date(wf.created_at).toLocaleString()}
              </td>
              <td className="px-4 py-3">
                <ChevronRight size={16} className="text-slate-500" />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
