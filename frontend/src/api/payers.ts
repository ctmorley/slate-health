import client from "./client";
import type {
  PayerResponse,
  PayerRuleResponse,
  PayerRuleCreate,
  PayerRuleUpdate,
} from "../types";

export async function listPayers(): Promise<PayerResponse[]> {
  const { data } = await client.get<PayerResponse[]>("/api/v1/payers");
  return data;
}

export async function getPayer(payerId: string): Promise<PayerResponse> {
  const { data } = await client.get<PayerResponse>(
    `/api/v1/payers/${payerId}`,
  );
  return data;
}

export async function listPayerRules(
  payerId: string,
): Promise<PayerRuleResponse[]> {
  const { data } = await client.get<PayerRuleResponse[]>(
    `/api/v1/payers/${payerId}/rules`,
  );
  return data;
}

export async function createPayerRule(
  payerId: string,
  rule: PayerRuleCreate,
): Promise<PayerRuleResponse> {
  const { data } = await client.post<PayerRuleResponse>(
    `/api/v1/payers/${payerId}/rules`,
    rule,
  );
  return data;
}

export async function updatePayerRule(
  payerId: string,
  ruleId: string,
  update: PayerRuleUpdate,
): Promise<PayerRuleResponse> {
  const { data } = await client.put<PayerRuleResponse>(
    `/api/v1/payers/${payerId}/rules/${ruleId}`,
    update,
  );
  return data;
}
