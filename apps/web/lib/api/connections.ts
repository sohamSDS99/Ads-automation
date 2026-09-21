/**
 * Connections: which sources this workspace uses.
 *
 * Every function here reads or switches. There is deliberately nothing that
 * sends a credential — the API has no endpoint that accepts one, because a key
 * lives in the deployment's environment and a workspace's only say is whether
 * it may be used.
 */
import { apiFetch } from "@/lib/api";

export type SourceKind = "openrouter" | "google_ads" | "dataforseo" | "webshare" | "smtp";

/** One account a Google Ads grant reaches, as a successful test recorded it. */
export type AccessibleAccount = {
  customer_id: string;
  name: string | null;
  manager: boolean;
};

export type Source = {
  kind: SourceKind;
  label: string;
  description: string;
  /** True when a run cannot start without it. Only the model surface is. */
  required_for_runs: boolean;

  /** Every variable this source reads, in the order the screen lists them. */
  env_vars: string[];
  /** The required ones this deployment has not set — the exact list to add. */
  missing_env_vars: string[];
  /** Whether the deployment supplies enough to use this source at all. */
  configured: boolean;

  /** Whether this workspace has switched it on. */
  connected: boolean;
  connected_at: string | null;
  connected_by: string | null;
  connected_by_name: string | null;

  last_tested_at: string | null;
  last_test_ok: boolean | null;
  last_test_detail: string | null;
  /** Masked hints only. `accessible` is a list, everything else is a scalar. */
  meta: Record<string, string | number | boolean | null | AccessibleAccount[]>;
};

export type SourceTest = {
  kind: SourceKind;
  ok: boolean;
  detail: string;
  meta: Record<string, unknown>;
  tested_at: string;
  /** False for a disconnected source: there is no row to remember the verdict. */
  recorded: boolean;
};

export function listConnections(): Promise<{ sources: Source[] }> {
  return apiFetch("/connections");
}

/**
 * Switch a source on.
 *
 * No body. The deployment already holds the credential; this records that this
 * workspace may spend it, and the API tests it on the way through — which is
 * why the response is the whole card and not an acknowledgement.
 */
export function connectSource(kind: SourceKind): Promise<Source> {
  return apiFetch(`/connections/${kind}/connect`, { method: "POST" });
}

export function disconnectSource(kind: SourceKind): Promise<void> {
  return apiFetch(`/connections/${kind}`, { method: "DELETE" });
}

export function testSource(kind: SourceKind): Promise<SourceTest> {
  return apiFetch(`/connections/${kind}/test`, { method: "POST" });
}
