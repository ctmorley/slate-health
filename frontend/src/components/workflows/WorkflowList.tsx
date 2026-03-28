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
      return <CheckCircle2 size={14} className="text-green-600" />;
    case "failed":
    case "terminated":
      return <XCircle size={14} className="text-red-600" />;
    case "running":
      return <Loader2 size={14} className="animate-spin text-blue-600" />;
    case "cancelled":
    case "canceled":
      return <XCircle size={14} className="text-gray-500" />;
    default:
      return <Clock size={14} className="text-yellow-600" />;
  }
}

function statusBadgeClass(status: string): string {
  switch (status) {
    case "completed":
      return "bg-green-100 text-green-800";
    case "failed":
    case "terminated":
      return "bg-red-100 text-red-800";
    case "running":
      return "bg-blue-100 text-blue-800";
    case "cancelled":
    case "canceled":
      return "bg-gray-100 text-gray-600";
    default:
      return "bg-yellow-100 text-yellow-800";
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
        <div className="h-6 w-6 animate-spin rounded-full border-4 border-teal-600 border-t-transparent" />
      </div>
    );
  }

  if (workflows.length === 0) {
    return (
      <div
        className="rounded-lg border border-gray-200 bg-white py-12 text-center text-sm text-gray-400"
        data-testid="workflow-list-empty"
      >
        No workflow executions found
      </div>
    );
  }

  return (
    <div
      className="overflow-hidden rounded-lg border border-gray-200 bg-white"
      data-testid="workflow-list"
    >
      <table className="w-full text-left text-sm">
        <thead className="border-b border-gray-200 bg-gray-50">
          <tr>
            <th className="px-4 py-3 font-medium text-gray-600">
              Workflow ID
            </th>
            <th className="px-4 py-3 font-medium text-gray-600">Agent</th>
            <th className="px-4 py-3 font-medium text-gray-600">Status</th>
            <th className="hidden px-4 py-3 font-medium text-gray-600 md:table-cell">
              Duration
            </th>
            <th className="hidden px-4 py-3 font-medium text-gray-600 lg:table-cell">
              Started
            </th>
            <th className="w-10 px-4 py-3" />
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {workflows.map((wf) => (
            <tr
              key={wf.id}
              onClick={() => onSelectWorkflow(wf)}
              className="cursor-pointer transition-colors hover:bg-gray-50"
              data-testid={`workflow-row-${wf.id}`}
            >
              <td className="px-4 py-3">
                <div className="flex items-center gap-2">
                  <Play size={14} className="text-gray-400" />
                  <span className="font-mono text-xs text-gray-700">
                    {wf.workflow_id.slice(0, 16)}
                  </span>
                </div>
              </td>
              <td className="px-4 py-3 text-gray-700">
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
              <td className="hidden px-4 py-3 text-xs text-gray-500 md:table-cell">
                {formatDuration(wf.created_at, wf.updated_at)}
              </td>
              <td className="hidden px-4 py-3 text-xs text-gray-500 lg:table-cell">
                {new Date(wf.created_at).toLocaleString()}
              </td>
              <td className="px-4 py-3">
                <ChevronRight size={16} className="text-gray-400" />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
