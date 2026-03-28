import { useState, useEffect, useCallback } from "react";
import {
  Search,
  Download,
  Shield,
  ChevronLeft,
  ChevronRight,
  Filter,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  listAuditLogs,
  exportAuditLogs,
  fetchAuditFilterOptions,
  type AuditLogParams,
} from "@/api/audit";
import type { AuditLogEntry } from "@/types";

const PAGE_SIZE = 20;

/**
 * Fallback values used while the dynamic filter-options endpoint loads or
 * if it fails.  These are kept in sync with the backend audit action
 * strings (see agent_service.py, review_queue.py, etc.).
 */
const FALLBACK_ACTIONS = [
  "agent_task_created",
  "agent_task_updated",
  "agent_task_deleted",
  "agent_task_cancelled",
  "agent_workflow_started",
  "hitl_review_created",
  "hitl_review_approved",
  "hitl_review_rejected",
  "hitl_review_escalated",
  "hitl_escalation_created",
  "phi_accessed",
];

const FALLBACK_RESOURCE_TYPES = [
  "agent_task",
  "hitl_review",
  "claim",
  "patient",
  "payer",
  "payer_rule",
  "user",
  "workflow",
];

export default function AuditLogViewer() {
  const [logs, setLogs] = useState<AuditLogEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);

  // Dynamic filter options fetched from backend
  const [actionOptions, setActionOptions] = useState<string[]>(FALLBACK_ACTIONS);
  const [resourceTypeOptions, setResourceTypeOptions] = useState<string[]>(FALLBACK_RESOURCE_TYPES);

  // Filters
  const [actionFilter, setActionFilter] = useState("");
  const [resourceTypeFilter, setResourceTypeFilter] = useState("");
  const [actorIdFilter, setActorIdFilter] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [searchText, setSearchText] = useState("");
  const [searchDebounced, setSearchDebounced] = useState("");
  const [phiOnly, setPhiOnly] = useState(false);

  // Fetch dynamic filter options on mount
  useEffect(() => {
    fetchAuditFilterOptions()
      .then((opts) => {
        if (opts.actions.length > 0) setActionOptions(opts.actions);
        if (opts.resource_types.length > 0) setResourceTypeOptions(opts.resource_types);
      })
      .catch(() => {
        /* keep fallback values */
      });
  }, []);

  // Debounce search text → searchDebounced so we don't fire API on every keystroke
  useEffect(() => {
    const timer = setTimeout(() => setSearchDebounced(searchText), 300);
    return () => clearTimeout(timer);
  }, [searchText]);

  const buildParams = useCallback((): AuditLogParams => {
    const params: AuditLogParams = {};
    if (actionFilter) params.action = actionFilter;
    if (resourceTypeFilter) params.resource_type = resourceTypeFilter;
    if (actorIdFilter) params.actor_id = actorIdFilter;
    // Send full ISO datetimes to ensure correct boundary handling:
    // start_time at beginning of day, end_time at end of day
    if (startDate) params.start_time = `${startDate}T00:00:00`;
    if (endDate) params.end_time = `${endDate}T23:59:59.999999`;
    if (phiOnly) params.phi_accessed = true;
    if (searchDebounced) params.search = searchDebounced;
    return params;
  }, [actionFilter, resourceTypeFilter, actorIdFilter, startDate, endDate, phiOnly, searchDebounced]);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;

    async function fetchLogs() {
      setLoading(true);
      setError(null);
      try {
        const params: AuditLogParams = { ...buildParams(), limit: PAGE_SIZE, offset };
        const result = await listAuditLogs(params, controller.signal);
        if (active) {
          setLogs(result.items);
          setTotal(result.total);
        }
      } catch (err) {
        if (!active || controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : "Failed to load audit logs");
      } finally {
        if (active) setLoading(false);
      }
    }

    fetchLogs();
    return () => {
      active = false;
      controller.abort();
    };
  }, [offset, buildParams]);

  useEffect(() => {
    setOffset(0);
  }, [actionFilter, resourceTypeFilter, actorIdFilter, startDate, endDate, phiOnly, searchDebounced]);

  async function handleExport() {
    setExportError(null);
    try {
      const params = buildParams();
      const blob = await exportAuditLogs(params);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `audit-logs-${new Date().toISOString().slice(0, 10)}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setExportError(err instanceof Error ? err.message : "Failed to export audit logs");
    }
  }

  const totalPages = Math.ceil(total / PAGE_SIZE);
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <div data-testid="audit-log-viewer">
      {/* Filters — Row 1: search + action + resource type */}
      <div className="mb-3 flex flex-col gap-3 lg:flex-row lg:items-end">
        <div className="flex-1">
          <label className="mb-1 block text-xs font-medium text-gray-600">
            Search
          </label>
          <div className="relative">
            <Search
              size={16}
              className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400"
            />
            <input
              type="text"
              placeholder="Search by actor, action, resource..."
              value={searchText}
              onChange={(e) => setSearchText(e.target.value)}
              className="w-full rounded-md border border-gray-300 py-2 pl-9 pr-3 text-sm focus:border-teal-500 focus:outline-none focus:ring-1 focus:ring-teal-500"
              data-testid="audit-search"
            />
          </div>
        </div>

        <div>
          <label className="mb-1 block text-xs font-medium text-gray-600">
            Action Type
          </label>
          <select
            value={actionFilter}
            onChange={(e) => setActionFilter(e.target.value)}
            className="rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-teal-500 focus:outline-none focus:ring-1 focus:ring-teal-500"
            data-testid="audit-action-filter"
          >
            <option value="">All Actions</option>
            {actionOptions.map((a) => (
              <option key={a} value={a}>
                {a.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="mb-1 block text-xs font-medium text-gray-600">
            Resource Type
          </label>
          <select
            value={resourceTypeFilter}
            onChange={(e) => setResourceTypeFilter(e.target.value)}
            className="rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-teal-500 focus:outline-none focus:ring-1 focus:ring-teal-500"
            data-testid="audit-resource-filter"
          >
            <option value="">All Resources</option>
            {resourceTypeOptions.map((r) => (
              <option key={r} value={r}>
                {r.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Filters — Row 2: actor, dates, PHI, export */}
      <div className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-end">
        <div>
          <label className="mb-1 block text-xs font-medium text-gray-600">
            Actor ID
          </label>
          <input
            type="text"
            placeholder="Filter by actor UUID..."
            value={actorIdFilter}
            onChange={(e) => setActorIdFilter(e.target.value)}
            className="rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-teal-500 focus:outline-none focus:ring-1 focus:ring-teal-500"
            data-testid="audit-actor-filter"
          />
        </div>

        <div>
          <label className="mb-1 block text-xs font-medium text-gray-600">
            Start Date
          </label>
          <input
            type="date"
            value={startDate}
            onChange={(e) => setStartDate(e.target.value)}
            className="rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-teal-500 focus:outline-none focus:ring-1 focus:ring-teal-500"
            data-testid="audit-start-date"
          />
        </div>

        <div>
          <label className="mb-1 block text-xs font-medium text-gray-600">
            End Date
          </label>
          <input
            type="date"
            value={endDate}
            onChange={(e) => setEndDate(e.target.value)}
            className="rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-teal-500 focus:outline-none focus:ring-1 focus:ring-teal-500"
            data-testid="audit-end-date"
          />
        </div>

        <div className="flex items-end gap-2">
          <label className="flex items-center gap-1.5 rounded-md border border-gray-300 px-3 py-2 text-sm">
            <input
              type="checkbox"
              checked={phiOnly}
              onChange={(e) => setPhiOnly(e.target.checked)}
              className="h-4 w-4 rounded border-gray-300 text-teal-600 focus:ring-teal-500"
              data-testid="audit-phi-filter"
            />
            <Shield size={14} className="text-amber-600" />
            PHI only
          </label>
          <Button variant="outline" size="sm" onClick={handleExport} data-testid="audit-export-button">
            <Download size={14} />
            Export
          </Button>
        </div>
      </div>

      {/* Errors */}
      {error && (
        <div className="mb-4 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700" data-testid="audit-error">
          {error}
        </div>
      )}
      {exportError && (
        <div className="mb-4 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700" data-testid="audit-export-error">
          Export failed: {exportError}
        </div>
      )}

      {/* Loading */}
      {loading && logs.length === 0 ? (
        <div className="flex items-center justify-center py-12" data-testid="audit-loading">
          <div className="h-6 w-6 animate-spin rounded-full border-4 border-teal-600 border-t-transparent" />
        </div>
      ) : logs.length === 0 ? (
        <div
          className="rounded-lg border border-gray-200 bg-white py-12 text-center text-sm text-gray-400"
          data-testid="audit-empty"
        >
          No audit log entries found
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border border-gray-200 bg-white">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-gray-200 bg-gray-50">
              <tr>
                <th className="px-4 py-3 font-medium text-gray-600">
                  Timestamp
                </th>
                <th className="px-4 py-3 font-medium text-gray-600">Actor</th>
                <th className="px-4 py-3 font-medium text-gray-600">Action</th>
                <th className="hidden px-4 py-3 font-medium text-gray-600 md:table-cell">
                  Resource
                </th>
                <th className="hidden px-4 py-3 font-medium text-gray-600 lg:table-cell">
                  Resource ID
                </th>
                <th className="px-4 py-3 font-medium text-gray-600">PHI</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {logs.map((log) => (
                <tr
                  key={log.id}
                  className="hover:bg-gray-50"
                  data-testid={`audit-row-${log.id}`}
                >
                  <td className="px-4 py-3 text-xs text-gray-600">
                    {log.timestamp ? new Date(log.timestamp).toLocaleString() : "-"}
                  </td>
                  <td className="px-4 py-3 text-gray-700">
                    {log.actor_id ?? "system"}
                  </td>
                  <td className="px-4 py-3">
                    <span className="inline-flex items-center rounded-md bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-700">
                      {log.action ?? "-"}
                    </span>
                  </td>
                  <td className="hidden px-4 py-3 text-gray-600 md:table-cell">
                    {log.resource_type ?? "-"}
                  </td>
                  <td className="hidden px-4 py-3 font-mono text-xs text-gray-500 lg:table-cell">
                    {log.resource_id?.slice(0, 12) ?? "-"}
                  </td>
                  <td className="px-4 py-3">
                    {log.phi_accessed ? (
                      <Shield size={14} className="text-amber-600" />
                    ) : (
                      <span className="text-gray-300">-</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

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
