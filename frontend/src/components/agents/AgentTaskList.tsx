import { useState, useEffect } from "react";
import {
  Clock,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Loader2,
  Search,
  ChevronLeft,
  ChevronRight,
  Ban,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { listTasks } from "@/api/agents";
import type { AgentType, AgentTaskResponse, TaskStatus } from "@/types";

interface AgentTaskListProps {
  agentType: AgentType;
  onSelectTask: (task: AgentTaskResponse) => void;
}

const STATUS_CONFIG: Record<
  TaskStatus,
  { icon: React.ReactNode; label: string; badgeClass: string }
> = {
  pending: {
    icon: <Clock size={14} />,
    label: "Pending",
    badgeClass: "bg-yellow-500/10 text-yellow-400",
  },
  running: {
    icon: <Loader2 size={14} className="animate-spin" />,
    label: "Running",
    badgeClass: "bg-blue-500/10 text-blue-400",
  },
  completed: {
    icon: <CheckCircle2 size={14} />,
    label: "Completed",
    badgeClass: "bg-mint-600/10 text-mint-500",
  },
  failed: {
    icon: <XCircle size={14} />,
    label: "Failed",
    badgeClass: "bg-coral-600/10 text-coral-500",
  },
  review: {
    icon: <AlertTriangle size={14} />,
    label: "In Review",
    badgeClass: "bg-orange-500/10 text-orange-400",
  },
  cancelled: {
    icon: <Ban size={14} />,
    label: "Cancelled",
    badgeClass: "bg-slate-500/10 text-slate-400",
  },
};

const PAGE_SIZE = 10;

export default function AgentTaskList({
  agentType,
  onSelectTask,
}: AgentTaskListProps) {
  const [tasks, setTasks] = useState<AgentTaskResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [search, setSearch] = useState("");
  const [searchDebounced, setSearchDebounced] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Debounce search text so we don't fire API on every keystroke
  useEffect(() => {
    const timer = setTimeout(() => setSearchDebounced(search), 300);
    return () => clearTimeout(timer);
  }, [search]);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;

    async function fetchTasks() {
      setLoading(true);
      setError(null);
      try {
        const params: { limit: number; offset: number; status_filter?: string; start_date?: string; end_date?: string; search?: string } = {
          limit: PAGE_SIZE,
          offset,
        };
        if (statusFilter) params.status_filter = statusFilter;
        if (startDate) params.start_date = `${startDate}T00:00:00`;
        if (endDate) params.end_date = `${endDate}T23:59:59.999999`;
        if (searchDebounced) params.search = searchDebounced;
        const result = await listTasks(agentType, params, controller.signal);
        if (active) {
          setTasks(result.items);
          setTotal(result.total);
        }
      } catch (err) {
        if (!active || controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : "Failed to load tasks");
      } finally {
        if (active) setLoading(false);
      }
    }

    fetchTasks();
    return () => {
      active = false;
      controller.abort();
    };
  }, [agentType, offset, statusFilter, startDate, endDate, searchDebounced]);

  // Reset offset when filters change
  useEffect(() => {
    setOffset(0);
  }, [statusFilter, startDate, endDate, agentType, searchDebounced]);

  const totalPages = Math.ceil(total / PAGE_SIZE);
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  if (loading && tasks.length === 0) {
    return (
      <div className="flex items-center justify-center py-12" data-testid="task-list-loading">
        <div className="h-6 w-6 animate-spin rounded-full border-4 border-accent-700 border-t-transparent" />
      </div>
    );
  }

  if (error) {
    return (
      <div
        className="glass-card rounded-lg p-4 text-sm text-coral-500"
        data-testid="task-list-error"
      >
        {error}
      </div>
    );
  }

  return (
    <div data-testid="agent-task-list">
      {/* Filters */}
      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center">
        <div className="relative flex-1">
          <Search
            size={16}
            className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500"
          />
          <input
            type="text"
            placeholder="Search by task ID or patient ID..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="dark-input w-full pl-9"
            data-testid="task-search-input"
          />
        </div>
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="dark-select"
          data-testid="task-status-filter"
        >
          <option value="">All Statuses</option>
          <option value="pending">Pending</option>
          <option value="running">Running</option>
          <option value="completed">Completed</option>
          <option value="failed">Failed</option>
          <option value="review">In Review</option>
          <option value="cancelled">Cancelled</option>
        </select>
        <input
          type="date"
          value={startDate}
          onChange={(e) => setStartDate(e.target.value)}
          className="dark-input"
          data-testid="task-start-date"
          placeholder="Start date"
        />
        <input
          type="date"
          value={endDate}
          onChange={(e) => setEndDate(e.target.value)}
          className="dark-input"
          data-testid="task-end-date"
          placeholder="End date"
        />
      </div>

      {/* Task list */}
      {tasks.length === 0 ? (
        <div
          className="glass-card rounded-lg py-12 text-center text-sm text-slate-500"
          data-testid="task-list-empty"
        >
          No tasks found
        </div>
      ) : (
        <div className="glass-card overflow-hidden rounded-lg">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-glass bg-slate_d-800">
              <tr>
                <th className="px-4 py-3 font-medium text-slate-400">
                  Task ID
                </th>
                <th className="px-4 py-3 font-medium text-slate-400">Status</th>
                <th className="hidden px-4 py-3 font-medium text-slate-400 md:table-cell">
                  Patient
                </th>
                <th className="hidden px-4 py-3 font-medium text-slate-400 lg:table-cell">
                  Confidence
                </th>
                <th className="px-4 py-3 font-medium text-slate-400">
                  Created
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-glass">
              {tasks.map((task) => {
                const cfg =
                  STATUS_CONFIG[task.status] ?? STATUS_CONFIG.pending;
                return (
                  <tr
                    key={task.id}
                    onClick={() => onSelectTask(task)}
                    className="cursor-pointer transition-colors hover:bg-slate_d-700"
                    data-testid={`task-row-${task.id}`}
                  >
                    <td className="px-4 py-3 font-mono text-xs text-slate-300">
                      {task.task_id.slice(0, 12)}...
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${cfg.badgeClass}`}
                        data-testid={`status-badge-${task.status}`}
                      >
                        {cfg.icon}
                        {cfg.label}
                      </span>
                    </td>
                    <td className="hidden px-4 py-3 text-slate-400 md:table-cell">
                      {task.patient_id?.slice(0, 12) ?? "-"}
                    </td>
                    <td className="hidden px-4 py-3 lg:table-cell">
                      {task.confidence_score != null ? (
                        <span className="text-slate-300">
                          {(task.confidence_score * 100).toFixed(0)}%
                        </span>
                      ) : (
                        <span className="text-slate-500">-</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-400">
                      {task.created_at
                        ? new Date(task.created_at).toLocaleDateString()
                        : "-"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="mt-4 flex items-center justify-between">
          <p className="text-sm text-slate-400">
            Showing {offset + 1}--{Math.min(offset + PAGE_SIZE, total)} of{" "}
            {total}
          </p>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={currentPage <= 1}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              <ChevronLeft size={16} />
              Prev
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={currentPage >= totalPages}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              Next
              <ChevronRight size={16} />
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
