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
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-accent-700 border-t-transparent" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="glass-card rounded-lg p-6 text-center text-sm text-coral-500">
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
          className="rounded-lg border border-yellow-600/30 bg-yellow-600/10 px-4 py-2 text-sm text-yellow-400"
        >
          <strong>Live update error:</strong> {liveUpdateError}
        </div>
      )}
      {failedMetricAgents.length > 0 && (
        <div
          role="alert"
          data-testid="metrics-partial-warning"
          className="flex items-center justify-between rounded-lg border border-yellow-600/30 bg-yellow-600/10 px-4 py-2 text-sm text-yellow-400"
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
            className="ml-3 shrink-0 rounded border border-yellow-600/30 bg-yellow-600/20 px-2 py-1 text-xs font-medium text-yellow-300 hover:bg-yellow-600/30 disabled:opacity-50"
          >
            {retryingMetrics ? "Retrying..." : "Retry"}
          </button>
        </div>
      )}
      <div>
        <h1 className="font-display text-xl font-semibold text-slate-100">Dashboard</h1>
        <p className="text-sm text-slate-400">
          {summary
            ? `${summary.total_tasks} total tasks across ${AGENT_TYPES.length} agents`
            : "Overview of all agent activity"}
        </p>
      </div>

      {/* Summary stats bar */}
      {summary && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7">
          {[
            { label: "Total", value: summary.total_tasks, color: "text-slate-100" },
            { label: "Pending", value: summary.pending, color: "text-yellow-400" },
            { label: "Running", value: summary.running, color: "text-blue-400" },
            { label: "Completed", value: summary.completed, color: "text-mint-500" },
            { label: "Failed", value: summary.failed, color: "text-coral-500" },
            { label: "In Review", value: summary.in_review, color: "text-orange-400" },
            { label: "Cancelled", value: summary.cancelled, color: "text-slate-500" },
          ].map(({ label, value, color }) => (
            <div
              key={label}
              className="glass-card glass-card-hover rounded-lg px-3 py-2 text-center"
            >
              <p className={`text-lg font-semibold ${color}`}>{value}</p>
              <p className="text-xs text-slate-400">{label}</p>
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
