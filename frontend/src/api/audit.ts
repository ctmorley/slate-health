import client from "./client";
import type { AuditLogList, AuditFilterOptionsResponse } from "../types";

export interface AuditLogParams {
  limit?: number;
  offset?: number;
  action?: string;
  resource_type?: string;
  resource_id?: string;
  actor_id?: string;
  start_time?: string;
  end_time?: string;
  phi_accessed?: boolean;
  search?: string;
}

/** @deprecated Use AuditFilterOptionsResponse from ../types instead */
export type AuditFilterOptions = AuditFilterOptionsResponse;

export async function listAuditLogs(
  params?: AuditLogParams,
  signal?: AbortSignal,
): Promise<AuditLogList> {
  const { data } = await client.get<AuditLogList>("/api/v1/audit/logs", {
    params,
    signal,
  });
  return data;
}

export async function fetchAuditFilterOptions(): Promise<AuditFilterOptions> {
  const { data } = await client.get<AuditFilterOptions>(
    "/api/v1/audit/filter-options",
  );
  return data;
}

export async function exportAuditLogs(
  params?: AuditLogParams,
): Promise<Blob> {
  const { data } = await client.get<Blob>("/api/v1/audit/logs", {
    params: { ...params, format: "csv", limit: 500 },
    responseType: "blob",
  });
  return data;
}
