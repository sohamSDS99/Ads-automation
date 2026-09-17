/**
 * The only way this app talks to the API.
 *
 * Relative paths only. `next.config.ts` rewrites /api/v1/* to the API over the
 * private network, so the app is same-origin in every environment. No absolute
 * API address is ever compiled into client code, and no public env var carries
 * one (PRD §13.1).
 */
import type { Permission, Role } from "@/lib/permissions";

export const API_BASE = "/api/v1";

const CSRF_COOKIE = "csrf";
const CSRF_HEADER = "X-CSRF-Token";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

/** One field the API refused, from a `validation-failed` problem. */
export type ValidationError = {
  loc: (string | number)[];
  msg: string;
  type: string;
};

/** An RFC 9457 problem document. Every error this API emits takes this shape. */
export type Problem = {
  type: string;
  title: string;
  status: number;
  detail: string;
  instance?: string;
  missing_permission?: Permission;
  retry_after_seconds?: number;
  state?: string;
  errors?: ValidationError[];
};

/**
 * Pydantic prefixes a message raised from a validator with "Value error, ".
 * The sentence after it is the one written for a person to read.
 */
function fieldMessage(error: ValidationError): string {
  return error.msg.replace(/^Value error,\s*/i, "");
}

export class ApiError extends Error {
  readonly status: number;
  readonly problem: Problem | null;

  constructor(status: number, problem: Problem | null, fallback: string) {
    super(problem?.detail ?? fallback);
    this.name = "ApiError";
    this.status = status;
    this.problem = problem;
  }

  /**
   * The sentence to show the person who triggered this.
   *
   * A validation failure carries the generic "did not match the expected shape"
   * in `detail` and the useful message in `errors[]`. Showing the generic one
   * throws away the only part a person can act on — the schedule editor said
   * "the request body did not match the expected shape" where the API had
   * written "'60' is outside 0-59 for minute".
   */
  get detail(): string {
    const first = this.problem?.errors?.[0];
    if (first) return fieldMessage(first);
    return this.problem?.detail ?? this.message;
  }

  /** Every field the API refused, for a form that marks more than one. */
  get fieldErrors(): ValidationError[] {
    return this.problem?.errors ?? [];
  }

  get missingPermission(): Permission | undefined {
    return this.problem?.missing_permission;
  }

  get isUnauthenticated(): boolean {
    return this.status === 401;
  }

  get isForbidden(): boolean {
    return this.status === 403;
  }
}

function readCookie(name: string): string | undefined {
  if (typeof document === "undefined") return undefined;
  return document.cookie
    .split("; ")
    .find((entry) => entry.startsWith(`${name}=`))
    ?.slice(name.length + 1);
}

/**
 * Make sure a CSRF cookie exists before a write.
 *
 * Only ever needed on `/login` and `/invite/[token]`: every other page was
 * rendered for a signed-in session, which already carries one.
 */
async function ensureCsrfToken(): Promise<string | undefined> {
  const existing = readCookie(CSRF_COOKIE);
  if (existing) return existing;
  await fetch(`${API_BASE}/auth/csrf`, { credentials: "include" });
  return readCookie(CSRF_COOKIE);
}

export type ApiOptions = RequestInit & {
  /**
   * Send the browser to /login when the API says 401. On by default, and off
   * for the sign-in calls themselves, where a 401 means "wrong password" and
   * redirecting would just reload the page the user is already on.
   */
  redirectOn401?: boolean;
};

export async function apiFetch<T>(path: string, init: ApiOptions = {}): Promise<T> {
  const { redirectOn401 = true, ...request } = init;
  const method = (request.method ?? "GET").toUpperCase();

  const headers = new Headers(request.headers);
  headers.set("Accept", "application/json");
  if (!SAFE_METHODS.has(method)) {
    const token = await ensureCsrfToken();
    if (token) headers.set(CSRF_HEADER, token);
    // A FormData body is left alone: the browser writes its own
    // `multipart/form-data` header with the boundary, and setting one here
    // would send a boundary-less content type the server cannot parse.
    if (request.body && !headers.has("Content-Type") && !(request.body instanceof FormData)) {
      headers.set("Content-Type", "application/json");
    }
  }

  const response = await fetch(`${API_BASE}${path}`, {
    ...request,
    method,
    headers,
    credentials: "include",
  });

  const text = await response.text();
  const body: unknown = text ? JSON.parse(text) : null;

  if (!response.ok) {
    const problem = isProblem(body) ? body : null;
    if (response.status === 401 && redirectOn401 && typeof window !== "undefined") {
      const next = window.location.pathname + window.location.search;
      window.location.assign(`/login?next=${encodeURIComponent(next)}`);
    }
    throw new ApiError(response.status, problem, `${method} ${path} → ${response.status}`);
  }
  return body as T;
}

function isProblem(body: unknown): body is Problem {
  return typeof body === "object" && body !== null && "title" in body && "status" in body;
}

// --- response shapes, mirroring agent.api.schemas_auth ----------------------

export type Health = {
  status: "ok" | "degraded";
  db: "ok" | "error";
  redis: "ok" | "error";
  version: string;
};

export type Me = {
  id: string;
  email: string;
  name: string;
  role: Role;
  permissions: Permission[];
  workspace_id: string;
  workspace_name: string;
};

export type UserSummary = {
  id: string;
  email: string;
  name: string;
  role: Role;
  status: "invited" | "active" | "disabled";
  last_login_at: string | null;
  created_at: string;
};

export type InvitePreview = {
  state: "valid" | "invalid" | "expired" | "accepted";
  email: string | null;
  role: Role | null;
  workspace_name: string | null;
  expires_at: string | null;
};

export function getHealth(): Promise<Health> {
  return apiFetch<Health>("/health");
}

export function getMe(): Promise<Me> {
  return apiFetch<Me>("/auth/me", { redirectOn401: false });
}

export function login(email: string, password: string): Promise<Me> {
  return apiFetch<Me>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
    redirectOn401: false,
  });
}

export function logout(): Promise<void> {
  return apiFetch<void>("/auth/logout", { method: "POST" });
}

export function acceptInvite(token: string, name: string, password: string): Promise<Me> {
  return apiFetch<Me>(`/invites/${encodeURIComponent(token)}/accept`, {
    method: "POST",
    body: JSON.stringify({ name, password }),
    redirectOn401: false,
  });
}
