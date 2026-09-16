/**
 * The only way this app talks to the API.
 *
 * Relative paths only. `next.config.ts` rewrites /api/v1/* to the API over the
 * private network, so the app is same-origin in every environment. No absolute
 * API address is ever compiled into client code, and no public env var carries
 * one (PRD §13.1).
 */
export const API_BASE = "/api/v1";

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, message: string, body: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: "same-origin",
    headers: { Accept: "application/json", ...init?.headers },
  });

  const text = await response.text();
  const body: unknown = text ? JSON.parse(text) : null;

  if (!response.ok) {
    throw new ApiError(response.status, `${init?.method ?? "GET"} ${path} → ${response.status}`, body);
  }
  return body as T;
}

/** Mirrors `agent.api.schemas.HealthResponse`. */
export type Health = {
  status: "ok" | "degraded";
  db: "ok" | "error";
  redis: "ok" | "error";
  version: string;
};

export function getHealth(): Promise<Health> {
  return apiFetch<Health>("/health");
}
