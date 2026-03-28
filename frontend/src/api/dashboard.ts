import client from "./client";
import type { DashboardSummary, AgentMetrics, AgentType } from "../types";

export async function fetchDashboardSummary(): Promise<DashboardSummary> {
  const { data } = await client.get<DashboardSummary>(
    "/api/v1/dashboard/summary",
  );
  return data;
}

export async function fetchAgentMetrics(
  agentType: AgentType,
): Promise<AgentMetrics> {
  const { data } = await client.get<AgentMetrics>(
    `/api/v1/dashboard/agents/${agentType}/metrics`,
  );
  return data;
}
