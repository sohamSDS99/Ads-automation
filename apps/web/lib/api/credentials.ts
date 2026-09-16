/**
 * The key vault.
 *
 * Every function here writes or tests. There is deliberately no read: the API
 * has no endpoint that returns a secret, so neither does this module.
 */
import { apiFetch } from "@/lib/api";

export type CredentialKind = "openrouter" | "google_ads" | "dataforseo" | "smtp";
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
};

export type CredentialSummary = {
  id: string;
  kind: CredentialKind;
  scope: CredentialScope;
  project_id: string | null;
  user_id: string | null;
  meta: Record<string, string | number | boolean | null>;
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
