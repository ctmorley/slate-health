import client from "./client";
import type {
  AgentType,
  AgentTaskCreate,
  AgentTaskResponse,
  AgentTaskList,
  AgentStatsResponse,
} from "../types";

export async function createTask(
  agentType: AgentType,
  task: AgentTaskCreate,
): Promise<AgentTaskResponse> {
  const { data } = await client.post<AgentTaskResponse>(
    `/api/v1/agents/${agentType}/tasks`,
    task,
  );
  return data;
}

export async function listTasks(
  agentType: AgentType,
  params?: { limit?: number; offset?: number; status_filter?: string; start_date?: string; end_date?: string; search?: string },
  signal?: AbortSignal,
): Promise<AgentTaskList> {
  const { data } = await client.get<AgentTaskList>(
    `/api/v1/agents/${agentType}/tasks`,
    { params, signal },
  );
  return data;
}

export async function getTask(
  agentType: AgentType,
  taskId: string,
): Promise<AgentTaskResponse> {
  const { data } = await client.get<AgentTaskResponse>(
    `/api/v1/agents/${agentType}/tasks/${taskId}`,
  );
  return data;
}

export async function cancelTask(
  agentType: AgentType,
  taskId: string,
): Promise<void> {
  await client.post(`/api/v1/agents/${agentType}/tasks/${taskId}/cancel`);
}

export async function getAgentStats(
  agentType: AgentType,
): Promise<AgentStatsResponse> {
  const { data } = await client.get<AgentStatsResponse>(
    `/api/v1/agents/${agentType}/stats`,
  );
  return data;
}
