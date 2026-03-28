import { useCallback, useEffect, useRef, useState } from "react";
import { fetchDashboardSummary } from "../api/dashboard";
import { useWebSocket } from "./useWebSocket";
import {
  fetchAgentMetricsSafe,
  aggregateTasksByDay,
} from "./useDashboardData";
import type {
  DashboardSummary,
  RecentTaskSummary,
  AgentMetrics,
  AgentType,
  WsMessage,
} from "../types";
import { AGENT_TYPES } from "../types";

/** Minimum interval (ms) between dashboard summary API refetches triggered by WS events. */
const SUMMARY_REFETCH_THROTTLE_MS = 5_000;

export interface DashboardSyncOptions {
  setSummary: React.Dispatch<React.SetStateAction<DashboardSummary | null>>;
  setChartData: React.Dispatch<
    React.SetStateAction<{ date: string; count: number }[]>
  >;
  setRecentTasks: React.Dispatch<React.SetStateAction<RecentTaskSummary[]>>;
  setFailedMetricAgents: React.Dispatch<React.SetStateAction<AgentType[]>>;
}

/**
 * Hook that connects the WebSocket to the dashboard state.
 * Handles:
 *   - Prepending real-time task events to the activity feed (deduped)
 *   - Throttled refetch of summary + chart data on task changes
 *   - Tracking live update errors
 */
export function useDashboardSync({
  setSummary,
  setChartData,
  setRecentTasks,
  setFailedMetricAgents,
}: DashboardSyncOptions): { liveUpdateError: string | null } {
  const [liveUpdateError, setLiveUpdateError] = useState<string | null>(null);
  const lastRefetchRef = useRef(0);
  const pendingRefetchRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const isMountedRef = useRef(true);

  // Cleanup pending timers and mark unmounted to guard async state updates
  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
      if (pendingRefetchRef.current) {
        clearTimeout(pendingRefetchRef.current);
        pendingRefetchRef.current = null;
      }
    };
  }, []);

  const handleWsMessage = useCallback(
    (msg: WsMessage) => {
      if (msg.event === "task_status_changed") {
        const d = msg.data;
        const newTask: RecentTaskSummary = {
          id: d.task_id,
          task_id: d.task_id,
          agent_type: d.agent_type,
          status: d.status,
          confidence_score: null,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };
        setRecentTasks((prev) => {
          const deduped = prev.filter((t) => t.task_id !== d.task_id);
          return [newTask, ...deduped.slice(0, 19)];
        });

        // Throttled refresh of summary AND chart data
        const now = Date.now();
        const elapsed = now - lastRefetchRef.current;

        const refetchAll = () => {
          if (!isMountedRef.current) return;
          setLiveUpdateError(null);
          Promise.all([
            fetchDashboardSummary(),
            ...AGENT_TYPES.map((t) => fetchAgentMetricsSafe(t)),
          ])
            .then(([summaryData, ...agentResults]) => {
              if (!isMountedRef.current) return;
              setSummary(summaryData as DashboardSummary);
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
            })
            .catch((err) => {
              if (!isMountedRef.current) return;
              const message =
                err instanceof Error
                  ? err.message
                  : "Failed to refresh dashboard data";
              console.error(
                "[useDashboardSync] live update refetch failed:",
                message,
              );
              setLiveUpdateError(message);
            });
        };

        if (elapsed >= SUMMARY_REFETCH_THROTTLE_MS) {
          lastRefetchRef.current = now;
          refetchAll();
        } else if (!pendingRefetchRef.current) {
          pendingRefetchRef.current = setTimeout(() => {
            pendingRefetchRef.current = null;
            lastRefetchRef.current = Date.now();
            refetchAll();
          }, SUMMARY_REFETCH_THROTTLE_MS - elapsed);
        }
      }
    },
    [setSummary, setChartData, setRecentTasks, setFailedMetricAgents],
  );

  useWebSocket({ onMessage: handleWsMessage });

  return { liveUpdateError };
}
