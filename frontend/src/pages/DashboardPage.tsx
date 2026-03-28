import AgentStatusCards from "../components/dashboard/AgentStatusCards";
import MetricsChart from "../components/dashboard/MetricsChart";
import RecentActivity from "../components/dashboard/RecentActivity";
import { useDashboardData, useMetricsRetry } from "../hooks/useDashboardData";
import { useDashboardSync } from "../hooks/useDashboardSync";
import { AGENT_TYPES, AGENT_LABELS } from "../types";

const MAX_AUTO_RETRY = 3;

export default function DashboardPage() {
  const {
    summary,
    chartData,
    recentTasks,
    loading,
    error,
    failedMetricAgents,
    setSummary,
    setChartData,
    setRecentTasks,
    setFailedMetricAgents,
  } = useDashboardData();

  const { retryingMetrics, retryAttemptCount, retryFailedMetrics } =
    useMetricsRetry(
      failedMetricAgents,
      loading,
      setChartData,
      setFailedMetricAgents,
    );

  const { liveUpdateError } = useDashboardSync({
    setSummary,
    setChartData,
    setRecentTasks,
    setFailedMetricAgents,
  });

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-teal-600 border-t-transparent" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-lg border border-red-200 bg-red-50 p-6 text-center text-sm text-red-700">
        {error}
      </div>
    );
  }

  // Ensure all 6 agent types are represented even if API returns fewer
  const agentStats =
    summary?.agents ??
    AGENT_TYPES.map((t) => ({
      agent_type: t,
      total_tasks: 0,
      pending: 0,
      running: 0,
      completed: 0,
      failed: 0,
      in_review: 0,
      cancelled: 0,
      avg_confidence: null,
    }));

  const fullAgents = AGENT_TYPES.map(
    (t) =>
      agentStats.find((a) => a.agent_type === t) ?? {
        agent_type: t,
        total_tasks: 0,
        pending: 0,
        running: 0,
        completed: 0,
        failed: 0,
        in_review: 0,
        cancelled: 0,
        avg_confidence: null,
      },
  );

  return (
    <div className="space-y-6">
      {liveUpdateError && (
        <div
          role="alert"
          className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-800"
        >
          <strong>Live update error:</strong> {liveUpdateError}
        </div>
      )}
      {failedMetricAgents.length > 0 && (
        <div
          role="alert"
          data-testid="metrics-partial-warning"
          className="flex items-center justify-between rounded-lg border border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-800"
        >
          <span>
            <strong>Incomplete chart data:</strong> Failed to load metrics for{" "}
            {failedMetricAgents.map((a) => AGENT_LABELS[a]).join(", ")}.
            {retryAttemptCount < MAX_AUTO_RETRY
              ? " Retrying automatically..."
              : " Chart may not reflect all agent activity."}
          </span>
          <button
            data-testid="metrics-retry-button"
            onClick={retryFailedMetrics}
            disabled={retryingMetrics}
            className="ml-3 shrink-0 rounded border border-amber-300 bg-amber-100 px-2 py-1 text-xs font-medium text-amber-900 hover:bg-amber-200 disabled:opacity-50"
          >
            {retryingMetrics ? "Retrying…" : "Retry"}
          </button>
        </div>
      )}
      <div>
        <h1 className="text-xl font-semibold text-gray-900">Dashboard</h1>
        <p className="text-sm text-gray-500">
          {summary
            ? `${summary.total_tasks} total tasks across ${AGENT_TYPES.length} agents`
            : "Overview of all agent activity"}
        </p>
      </div>

      {/* Summary stats bar */}
      {summary && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7">
          {[
            { label: "Total", value: summary.total_tasks, color: "text-gray-900" },
            { label: "Pending", value: summary.pending, color: "text-yellow-600" },
            { label: "Running", value: summary.running, color: "text-blue-600" },
            { label: "Completed", value: summary.completed, color: "text-green-600" },
            { label: "Failed", value: summary.failed, color: "text-red-600" },
            { label: "In Review", value: summary.in_review, color: "text-orange-600" },
            { label: "Cancelled", value: summary.cancelled, color: "text-gray-400" },
          ].map(({ label, value, color }) => (
            <div
              key={label}
              className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-center shadow-sm"
            >
              <p className={`text-lg font-semibold ${color}`}>{value}</p>
              <p className="text-xs text-gray-500">{label}</p>
            </div>
          ))}
        </div>
      )}

      {/* Agent status cards */}
      <AgentStatusCards agents={fullAgents} />

      {/* Chart + Recent Activity side by side */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <MetricsChart
          data={chartData}
          incompleteAgents={failedMetricAgents.map((a) => AGENT_LABELS[a])}
        />
        <RecentActivity tasks={recentTasks} />
      </div>
    </div>
  );
}
