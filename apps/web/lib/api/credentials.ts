/**
 * The key vault.
 *
 * Every function here writes or tests. There is deliberately no read: the API
 * has no endpoint that returns a secret, so neither does this module.
 */
import { apiFetch } from "@/lib/api";

export type CredentialKind =
  | "openrouter"
  | "google_ads"
  | "dataforseo"
  | "webshare"
  | "smtp";
export type CredentialScope = "workspace" | "project" | "user";

export type CredentialField = {
  name: string;
  label: string;
  required: boolean;
  /** Render as a password field. The API never sends its value back. */
  secret: boolean;
  hint: string;
};

export type CredentialKindInfo = {
  kind: CredentialKind;
  label: string;
  description: string;
  /** Which screen configures it: `settings` or `sources`. */
  where: "settings" | "sources";
  fields: CredentialField[];
  /** When set, this kind can be connected by consent instead of by typing. */
  oauth_provider: string | null;
  oauth_ready: boolean;
  /** The variable that supplies this kind when no row overrides it. */
  env_var: string | null;
  /** Whether that variable actually has a value in this deployment. */
  env_configured: boolean;
  env_last4: string | null;
  /** The fields consent supplies, which the form must therefore not ask for. */
  oauth_fields: string[];
};

/** One account a Google Ads grant reaches, as the callback recorded it. */
export type AccessibleAccount = {
  customer_id: string;
  name: string | null;
  manager: boolean;
};

export type CredentialSummary = {
  id: string;
  kind: CredentialKind;
  scope: CredentialScope;
  project_id: string | null;
  user_id: string | null;
  /** Masked hints only. `accessible` is a list, everything else is a scalar. */
  meta: Record<string, string | number | boolean | null | AccessibleAccount[]>;
  created_at: string;
  created_by: string;
  created_by_name: string | null;
  last_tested_at: string | null;
  last_test_ok: boolean | null;
};

export type CredentialTest = {
  id: string;
  kind: CredentialKind;
  ok: boolean;
  detail: string;
  meta: Record<string, string | number | boolean | null>;
  tested_at: string;
};

export function listCredentials(): Promise<{
  credentials: CredentialSummary[];
  kinds: CredentialKindInfo[];
}> {
  return apiFetch("/credentials");
}

export function createCredential(body: {
  kind: CredentialKind;
  scope?: CredentialScope;
  project_id?: string;
  values: Record<string, string>;
}): Promise<CredentialSummary> {
  return apiFetch("/credentials", { method: "POST", body: JSON.stringify(body) });
}

/**
 * Begin a Google Ads consent.
 *
 * Returns the URL to send the browser to — this does not navigate, because the
 * caller may want to handle the failure (no OAuth client configured, say)
 * without having left the page.
 */
/**
 * Test whatever currently supplies a kind — a stored row if the workspace has
 * one, the deployment's environment variable otherwise.
 *
 * `testCredential` needs an id, which an environment-supplied key does not
 * have. Asking "does my key work" should not require storing one first.
 */
export function testCredentialKind(kind: string): Promise<{
  kind: string;
  source: "vault" | "environment";
  ok: boolean;
  detail: string;
  meta: Record<string, unknown>;
  tested_at: string;
}> {
  return apiFetch(`/credentials/kinds/${kind}/test`, { method: "POST" });
}

export function startGoogleAdsOauth(body: {
  developer_token: string;
  return_to: string;
}): Promise<{ url: string }> {
  return apiFetch("/credentials/google-ads/authorize", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function testCredential(id: string): Promise<CredentialTest> {
  return apiFetch(`/credentials/${id}/test`, { method: "POST" });
}

export function deleteCredential(id: string): Promise<void> {
  return apiFetch(`/credentials/${id}`, { method: "DELETE" });
}

/** The credential of this kind that applies here, most specific first. */
export function credentialFor(
  credentials: CredentialSummary[],
  kind: CredentialKind,
  scope: CredentialScope = "workspace",
): CredentialSummary | undefined {
  return credentials.find((item) => item.kind === kind && item.scope === scope);
}
