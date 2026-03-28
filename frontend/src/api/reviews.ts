import client from "./client";
import type { ReviewList, ReviewResponse, ReviewActionRequest } from "../types";

export async function listReviews(params?: {
  limit?: number;
  offset?: number;
  status_filter?: string;
  agent_type?: string;
  task_id?: string;
}, signal?: AbortSignal): Promise<ReviewList> {
  const { data } = await client.get<ReviewList>("/api/v1/reviews", { params, signal });
  return data;
}

export async function getReview(reviewId: string): Promise<ReviewResponse> {
  const { data } = await client.get<ReviewResponse>(
    `/api/v1/reviews/${reviewId}`,
  );
  return data;
}

export async function approveReview(
  reviewId: string,
  req?: ReviewActionRequest,
): Promise<ReviewResponse> {
  const { data } = await client.post<ReviewResponse>(
    `/api/v1/reviews/${reviewId}/approve`,
    req ?? {},
  );
  return data;
}

export async function rejectReview(
  reviewId: string,
  req?: ReviewActionRequest,
): Promise<ReviewResponse> {
  const { data } = await client.post<ReviewResponse>(
    `/api/v1/reviews/${reviewId}/reject`,
    req ?? {},
  );
  return data;
}

export async function escalateReview(
  reviewId: string,
  req?: ReviewActionRequest,
): Promise<ReviewResponse> {
  const { data } = await client.post<ReviewResponse>(
    `/api/v1/reviews/${reviewId}/escalate`,
    req ?? {},
  );
  return data;
}
