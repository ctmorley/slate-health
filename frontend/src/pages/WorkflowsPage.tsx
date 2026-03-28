import { useState, useEffect } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { GitBranch, ChevronLeft, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import WorkflowList from "@/components/workflows/WorkflowList";
import WorkflowDetail from "@/components/workflows/WorkflowDetail";
import { listWorkflows } from "@/api/workflows";
import type { WorkflowExecutionResponse, AgentType } from "@/types";
import { AGENT_LABELS, AGENT_TYPES } from "@/types";

const PAGE_SIZE = 15;

export default function WorkflowsPage() {
  const { workflowId: routeWorkflowId } = useParams<{ workflowId?: string }>();
  const navigate = useNavigate();
  const [workflows, setWorkflows] = useState<WorkflowExecutionResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedWorkflow, setSelectedWorkflow] =
    useState<WorkflowExecutionResponse | null>(null);

  // Filters
  const [agentFilter, setAgentFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    let active = true;

    async function fetchWorkflows() {
      setLoading(true);
      setError(null);
      try {
        const params: {
          limit: number;
          offset: number;
          agent_type?: string;
          status_filter?: string;
        } = { limit: PAGE_SIZE, offset };
        if (agentFilter) params.agent_type = agentFilter;
        if (statusFilter) params.status_filter = statusFilter;
        const result = await listWorkflows(params, controller.signal);
        if (active) {
          setWorkflows(result.items);
          setTotal(result.total);
        }
      } catch (err) {
        if (!active || controller.signal.aborted) return;
        setError(
          err instanceof Error ? err.message : "Failed to load workflows",
        );
      } finally {
        if (active) setLoading(false);
      }
    }

    fetchWorkflows();
    return () => {
      active = false;
      controller.abort();
    };
  }, [offset, agentFilter, statusFilter]);

  useEffect(() => {
    setOffset(0);
  }, [agentFilter, statusFilter]);

  const totalPages = Math.ceil(total / PAGE_SIZE);
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  // Support route-based detail opening (e.g., /workflows/:workflowId)
  const detailWorkflowId = selectedWorkflow?.id ?? routeWorkflowId;

  if (detailWorkflowId) {
    return (
      <WorkflowDetail
        workflowId={detailWorkflowId}
        onBack={() => {
          setSelectedWorkflow(null);
          if (routeWorkflowId) navigate("/workflows");
        }}
      />
    );
  }

  return (
    <div>
      <div className="mb-6">
        <h1 className="flex items-center gap-2 text-xl font-semibold text-gray-900">
          <GitBranch size={22} />
          Workflows
        </h1>
        <p className="mt-1 text-sm text-gray-500">
          Temporal workflow executions across all agents.
        </p>
      </div>

      {/* Filters */}
      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center">
        <select
          value={agentFilter}
          onChange={(e) => setAgentFilter(e.target.value)}
          className="rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-teal-500 focus:outline-none focus:ring-1 focus:ring-teal-500"
          data-testid="wf-agent-filter"
        >
          <option value="">All Agents</option>
          {AGENT_TYPES.map((t) => (
            <option key={t} value={t}>
              {AGENT_LABELS[t]}
            </option>
          ))}
        </select>
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-teal-500 focus:outline-none focus:ring-1 focus:ring-teal-500"
          data-testid="wf-status-filter"
        >
          <option value="">All Statuses</option>
          <option value="running">Running</option>
          <option value="completed">Completed</option>
          <option value="failed">Failed</option>
          <option value="cancelled">Cancelled</option>
        </select>
      </div>

      {error && (
        <div className="mb-4 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
          {error}
        </div>
      )}

      <WorkflowList
        workflows={workflows}
        loading={loading}
        onSelectWorkflow={setSelectedWorkflow}
      />

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="mt-4 flex items-center justify-between">
          <p className="text-sm text-gray-500">
            Showing {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of{" "}
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
