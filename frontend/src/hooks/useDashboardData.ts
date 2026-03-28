import { useEffect, useState, useCallback, useRef } from "react";
import { fetchDashboardSummary, fetchAgentMetrics } from "../api/dashboard";
import type {
  DashboardSummary,
  RecentTaskSummary,
  AgentMetrics,
  AgentType,
} from "../types";
import { AGENT_TYPES } from "../types";

/** Maximum number of automatic retries for failed agent metrics. */
const MAX_AUTO_RETRY = 3;
/** Base delay (ms) for automatic retry backoff (doubles each attempt). */
const AUTO_RETRY_BASE_DELAY_MS = 2_000;

/**
 * Fetch metrics for a single agent, tracking success/failure.
 */
export async function fetchAgentMetricsSafe(
  agentType: AgentType,
): Promise<{ metrics: AgentMetrics | null; failedAgent: AgentType | null }> {
  try {
    const metrics = await fetchAgentMetrics(agentType);
    return { metrics, failedAgent: null };
  } catch {
    return { metrics: null, failedAgent: agentType };
  }
}

/**
 * Merge tasks_by_day arrays from multiple agents into a single
 * aggregate series summed by date.
 */
export function aggregateTasksByDay(
  metricsArr: (AgentMetrics | null)[],
): { date: string; count: number }[] {
  const byDate = new Map<string, number>();
  for (const m of metricsArr) {
    if (!m?.tasks_by_day) continue;
    for (const entry of m.tasks_by_day) {
      byDate.set(entry.date, (byDate.get(entry.date) ?? 0) + entry.count);
    }
  }
  return Array.from(byDate.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, count]) => ({ date, count }));
}

export interface DashboardData {
  summary: DashboardSummary | null;
  chartData: { date: string; count: number }[];
  recentTasks: RecentTaskSummary[];
  loading: boolean;
  error: string | null;
  failedMetricAgents: AgentType[];
}

export interface DashboardDataActions {
  setSummary: React.Dispatch<React.SetStateAction<DashboardSummary | null>>;
  setChartData: React.Dispatch<React.SetStateAction<{ date: string; count: number }[]>>;
  setRecentTasks: React.Dispatch<React.SetStateAction<RecentTaskSummary[]>>;
  setFailedMetricAgents: React.Dispatch<React.SetStateAction<AgentType[]>>;
}

/**
 * Hook that loads initial dashboard data (summary + all agent metrics).
 * Returns current state and setters for live-update consumers.
 */
export function useDashboardData(): DashboardData & DashboardDataActions {
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [chartData, setChartData] = useState<{ date: string; count: number }[]>([]);
  const [recentTasks, setRecentTasks] = useState<RecentTaskSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [failedMetricAgents, setFailedMetricAgents] = useState<AgentType[]>([]);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      setLoading(true);
      setError(null);
      try {
        const [summaryData, ...agentResults] = await Promise.all([
          fetchDashboardSummary(),
          ...AGENT_TYPES.map((t) => fetchAgentMetricsSafe(t)),
        ]);

        if (cancelled) return;
        setSummary(summaryData);

        const typedResults = agentResults as Array<{
          metrics: AgentMetrics | null;
          failedAgent: AgentType | null;
        }>;
        const metricsArr = typedResults.map((r) => r.metrics);
        const failed = typedResults
          .map((r) => r.failedAgent)
          .filter((a): a is AgentType => a !== null);
        setFailedMetricAgents(failed);

        const aggregated = aggregateTasksByDay(metricsArr);
        setChartData(aggregated);

        if (summaryData.recent_tasks) {
          setRecentTasks(summaryData.recent_tasks);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load dashboard");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    load();
    return () => {
      cancelled = true;
    };
  }, []);

  return {
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
  };
}

export interface MetricsRetryState {
  retryingMetrics: boolean;
  retryAttemptCount: number;
  retryFailedMetrics: () => Promise<void>;
}

/**
 * Hook that manages automatic + manual retry of failed agent metrics.
 * Implements exponential backoff for auto-retries up to MAX_AUTO_RETRY.
 */
export function useMetricsRetry(
  failedMetricAgents: AgentType[],
  loading: boolean,
  setChartData: React.Dispatch<React.SetStateAction<{ date: string; count: number }[]>>,
  setFailedMetricAgents: React.Dispatch<React.SetStateAction<AgentType[]>>,
): MetricsRetryState {
  const [retryingMetrics, setRetryingMetrics] = useState(false);
  const retryAttemptRef = useRef(0);
  const autoRetryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const retryFailedMetrics = useCallback(async () => {
    if (failedMetricAgents.length === 0 || retryingMetrics) return;
    setRetryingMetrics(true);
    try {
      const results = await Promise.all(
        failedMetricAgents.map((t) => fetchAgentMetricsSafe(t)),
      );
      const stillFailed = results
        .map((r) => r.failedAgent)
        .filter((a): a is AgentType => a !== null);
      const newMetrics = results
        .filter((r) => r.metrics !== null)
        .map((r) => r.metrics!);

      if (newMetrics.length > 0) {
        setChartData((prev) => {
          const merged = aggregateTasksByDay([
            { tasks_by_day: prev } as unknown as AgentMetrics,
            ...newMetrics,
          ]);
          return merged;
        });
      }
      setFailedMetricAgents(stillFailed);
      retryAttemptRef.current =
        stillFailed.length > 0 ? retryAttemptRef.current + 1 : 0;
    } finally {
      setRetryingMetrics(false);
    }
  }, [failedMetricAgents, retryingMetrics, setChartData, setFailedMetricAgents]);

  // Automatic retry with exponential backoff
  useEffect(() => {
    if (
      loading ||
      failedMetricAgents.length === 0 ||
      retryingMetrics ||
      retryAttemptRef.current >= MAX_AUTO_RETRY
    ) {
      return;
    }

    const delay =
      AUTO_RETRY_BASE_DELAY_MS * Math.pow(2, retryAttemptRef.current);
    autoRetryTimerRef.current = setTimeout(() => {
      retryFailedMetrics();
    }, delay);

    return () => {
      if (autoRetryTimerRef.current) {
        clearTimeout(autoRetryTimerRef.current);
        autoRetryTimerRef.current = null;
      }
    };
  }, [loading, failedMetricAgents, retryingMetrics, retryFailedMetrics]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (autoRetryTimerRef.current) {
        clearTimeout(autoRetryTimerRef.current);
        autoRetryTimerRef.current = null;
      }
    };
  }, []);

  return {
    retryingMetrics,
    retryAttemptCount: retryAttemptRef.current,
    retryFailedMetrics,
  };
}
