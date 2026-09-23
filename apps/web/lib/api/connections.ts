/**
 * Connections: which sources this workspace uses.
 *
 * Every function here reads or switches. There is deliberately nothing that
 * sends a credential — the API has no endpoint that accepts one, because a key
 * lives in the deployment's environment and a workspace's only say is whether
 * it may be used.
 *
 * Google Ads is the exception, and it is an exception that still types nothing.
 * Two of its five values are a person's rather than the deployment's, so
 * `authorizeGoogle` hands back a URL and the browser leaves for Google; what
 * comes back is a redirect to this app carrying `?google=connected` or
 * `?google=error&reason=…`. The secret itself never passes through here.
 */
import { apiFetch } from "@/lib/api";

export type SourceKind = "openrouter" | "google_ads" | "dataforseo" | "webshare" | "smtp";

/** One account a Google Ads grant reaches. */
export type AccessibleAccount = {
  customer_id: string;
  name: string | null;
  /** A manager (MCC) account. It holds no campaigns, so it is never the default. */
  manager: boolean;
  /** The manager to call this account through, or null when it is reached directly. */
  via_manager: string | null;
  currency: string | null;
};

/**
 * The half of a credential a person supplies, and what may be said about it.
 *
 * `granted` is deliberately not the card's `connected`: a deployment can hold
 * the developer token and the OAuth client — `configured` — while nobody has
 * signed in yet. Those two states have entirely different fixes, and one
 * boolean would name neither.
 */
export type SourceOAuth = {
  provider: string;
  /** The button, in the words the card shows. */
  action: string;
  explains: string;

  granted: boolean;
  granted_at: string | null;
  granted_by: string | null;
  granted_by_name: string | null;
  /** The Google account the grant was given by. */
  email: string | null;
  /** What Google actually granted, not what was asked for. */
  scopes: string[];

  accounts: AccessibleAccount[];
  customer_id: string | null;
  login_customer_id: string | null;
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

  /** Set only on a source a person's consent finishes. Null for the rest. */
  oauth: SourceOAuth | null;
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

/**
 * Begin a Google sign-in, and get the URL to send the browser to.
 *
 * Deliberately two steps rather than a link straight to Google: the consent URL
 * carries a one-use state the API mints and remembers, so it cannot be a static
 * `href`, and the API can refuse before anybody sees a consent screen that
 * could not have finished.
 *
 * `returnTo` is where Google's redirect lands afterwards. It is a path on this
 * app; anything naming another origin is replaced by the API.
 */
export function authorizeGoogle(returnTo: string): Promise<{ url: string }> {
  return apiFetch("/connections/google/authorize", {
    method: "POST",
    body: JSON.stringify({ return_to: returnTo }),
  });
}

/**
 * Point an existing grant at a different one of the accounts it reaches.
 *
 * One consent commonly reaches several accounts — anyone working out of a
 * manager account reaches all of its clients — so this corrects the callback's
 * guess without a second trip through Google.
 */
export function chooseAccount(kind: SourceKind, customerId: string): Promise<Source> {
  return apiFetch(`/connections/${kind}/account`, {
    method: "POST",
    body: JSON.stringify({ customer_id: customerId }),
  });
}
