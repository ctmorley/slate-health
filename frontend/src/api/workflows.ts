import client from "./client";
import type {
  WorkflowExecutionList,
  WorkflowExecutionResponse,
  WorkflowHistoryResponse,
} from "../types";

export async function listWorkflows(params?: {
  limit?: number;
  offset?: number;
  agent_type?: string;
  status_filter?: string;
}, signal?: AbortSignal): Promise<WorkflowExecutionList> {
  const { data } = await client.get<WorkflowExecutionList>(
    "/api/v1/workflows",
    { params, signal },
  );
  return data;
}

export async function getWorkflow(
  workflowId: string,
): Promise<WorkflowExecutionResponse> {
  const { data } = await client.get<WorkflowExecutionResponse>(
    `/api/v1/workflows/${workflowId}`,
  );
  return data;
}

export async function getWorkflowHistory(
  workflowId: string,
): Promise<WorkflowHistoryResponse> {
  const { data } = await client.get<WorkflowHistoryResponse>(
    `/api/v1/workflows/${workflowId}/history`,
  );
  return data;
}
