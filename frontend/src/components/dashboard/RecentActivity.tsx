import { Clock, CheckCircle2, XCircle, AlertTriangle, Loader2 } from "lucide-react";
import type { RecentTaskSummary, TaskStatus } from "../../types";
import { AGENT_LABELS } from "../../types";

interface RecentActivityProps {
  tasks: RecentTaskSummary[];
}

const STATUS_CONFIG: Record<
  TaskStatus,
  { icon: React.ReactNode; label: string; color: string }
> = {
  pending: {
    icon: <Clock size={14} />,
    label: "Pending",
    color: "text-yellow-600 bg-yellow-50",
  },
  running: {
    icon: <Loader2 size={14} className="animate-spin" />,
    label: "Running",
    color: "text-blue-600 bg-blue-50",
  },
  completed: {
    icon: <CheckCircle2 size={14} />,
    label: "Completed",
    color: "text-green-600 bg-green-50",
  },
  failed: {
    icon: <XCircle size={14} />,
    label: "Failed",
    color: "text-red-600 bg-red-50",
  },
  review: {
    icon: <AlertTriangle size={14} />,
    label: "In Review",
    color: "text-orange-600 bg-orange-50",
  },
  cancelled: {
    icon: <XCircle size={14} />,
    label: "Cancelled",
    color: "text-gray-500 bg-gray-50",
  },
};

function formatTimeAgo(dateStr: string | null): string {
  if (!dateStr) return "";
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

export default function RecentActivity({ tasks }: RecentActivityProps) {
  return (
    <div
      className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm"
      data-testid="recent-activity"
    >
      <h3 className="mb-3 text-sm font-semibold text-gray-900">
        Recent Activity
      </h3>

      {tasks.length === 0 ? (
        <p className="py-6 text-center text-sm text-gray-400">
          No recent activity
        </p>
      ) : (
        <ul className="space-y-2">
          {tasks.map((task) => {
            const cfg = STATUS_CONFIG[task.status] ?? STATUS_CONFIG.pending;
            return (
              <li
                key={task.id}
                data-task-id={task.task_id}
                className="flex items-center justify-between rounded-md px-3 py-2 text-sm hover:bg-gray-50"
              >
                <div className="flex items-center gap-2">
                  <span
                    className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${cfg.color}`}
                  >
                    {cfg.icon}
                    {cfg.label}
                  </span>
                  <span className="font-medium text-gray-700">
                    {AGENT_LABELS[task.agent_type] ?? task.agent_type}
                  </span>
                  <span className="hidden text-gray-400 sm:inline">
                    {task.id.slice(0, 8)}
                  </span>
                </div>
                <span className="text-xs text-gray-400">
                  {formatTimeAgo(task.updated_at ?? task.created_at)}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
