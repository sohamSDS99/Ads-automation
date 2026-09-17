"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CircleCheck, CircleX, Plug, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Field } from "@/components/ui/field";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import {
  createCredential,
  deleteCredential,
  startGoogleAdsOauth,
  testCredential,
  testCredentialKind,
  type AccessibleAccount,
  type CredentialKindInfo,
  type CredentialScope,
  type CredentialSummary,
} from "@/lib/api/credentials";
import { relativeTime } from "@/lib/format";
import { keys } from "@/lib/queries";
import { cn } from "@/lib/utils";

/**
 * One data source's credential: connect, test, replace, remove.
 *
 * There is no edit. A credential is write-only, so changing one means writing a
 * new one — which also means a half-typed replacement can never leave the old
 * key partly overwritten.
 */
export function CredentialCard({
  spec,
  credential,
  canWrite,
  reason,
  scope = "workspace",
}: {
  spec: CredentialKindInfo;
  credential: CredentialSummary | undefined;
  canWrite: boolean;
  /** Shown instead of the form when `canWrite` is false. */
  reason: string;
  /**
   * Which scope this card writes. `user` is the personal override on the
   * account screen — the only credential anyone may write for themselves.
   */
  scope?: CredentialScope;
}) {
  const queryClient = useQueryClient();
  const [values, setValues] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState(false);
  //: The escape hatch. Consent is the path for the account's owner; an operator
  //: holding five values from `scripts/google-ads-oauth.py` still needs a form.
  const [byHand, setByHand] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [error, setError] = useState<string | null>(null);
  //: Only for the environment-supplied case. A stored credential carries its own
  //: `last_test_ok`; a variable in a file has nowhere to write one back to, so
  //: the verdict lives for as long as the screen does and no longer.
  const [envVerdict, setEnvVerdict] = useState<"ok" | "failed" | null>(null);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: keys.credentials });

  const save = useMutation({
    mutationFn: () => createCredential({ kind: spec.kind, scope, values }),
    onSuccess: async (stored) => {
      setValues({});
      setEditing(false);
      setError(null);
      await invalidate();
      toast.success(`${spec.label} connected`, { description: "Testing it now." });
      test.mutate(stored.id);
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.detail : "The credential could not be stored."),
  });

  const test = useMutation({
    mutationFn: (id: string) => testCredential(id),
    onSuccess: async (result) => {
      await invalidate();
      if (result.ok) toast.success(`${spec.label} is working`, { description: result.detail });
      else toast.error(`${spec.label} did not answer`, { description: result.detail });
    },
    onError: (err) =>
      toast.error("The test could not run", {
        description: err instanceof ApiError ? err.detail : "Try again in a moment.",
      }),
  });

  //: The environment's key has no id, so it cannot go through `testCredential`.
  const testFromEnv = useMutation({
    mutationFn: () => testCredentialKind(spec.kind),
    onSuccess: (result) => {
      setEnvVerdict(result.ok ? "ok" : "failed");
      if (result.ok) toast.success(`${spec.label} is working`, { description: result.detail });
      else toast.error(`${spec.label} did not answer`, { description: result.detail });
    },
    onError: (err) =>
      toast.error("The test could not run", {
        description: err instanceof ApiError ? err.detail : "Try again in a moment.",
      }),
  });

  const remove = useMutation({
    mutationFn: (id: string) => deleteCredential(id),
    onSuccess: async () => {
      await invalidate();
      toast.success(`${spec.label} disconnected`);
    },
  });

  /**
   * Whether this kind is connected by consent rather than by typing.
   *
   * Read off the kind, not hardcoded: the API says which kinds have a provider
   * and which of their fields that provider supplies, so a second OAuth source
   * needs no change here.
   */
  const byConsent = Boolean(spec.oauth_provider) && !byHand;
  // A deployment with a Google OAuth client configured never shows the
  // paste-everything fallback: consent supplies five of the six values, and
  // offering a form for them alongside is offering a worse way to do the same
  // thing. It reappears only where consent cannot run at all.
  const asked = byConsent
    ? spec.fields.filter((field) => !spec.oauth_fields.includes(field.name))
    : spec.fields;

  const connect = useMutation({
    mutationFn: () =>
      startGoogleAdsOauth({
        developer_token: values.developer_token ?? "",
        // Come back to this screen, not to a default one: this card renders in
        // the setup wizard and in settings, and landing on the wrong one after
        // consent reads as having lost your place.
        return_to: `${window.location.pathname}${window.location.search}`,
      }),
    onSuccess: (started) => {
      // A full navigation, not a popup: Google refuses to render consent in an
      // iframe, and a popup is the thing browsers block.
      window.location.assign(started.url);
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.detail : "Google could not be reached."),
  });

  useOauthOutcome(spec, invalidate);

  const connected = Boolean(credential);
  /**
   * This source works with nothing typed, because the deployment put its key in
   * the environment and no workspace row overrides it.
   *
   * A stored credential still wins — `resolve_secret` looks in the vault first —
   * so this is only ever the state of a card with no row behind it.
   */
  const fromEnv = !connected && Boolean(spec.env_configured);
  // The form is not the default when the environment already answered: it is
  // the override, and it opens on request.
  const showForm = canWrite && (editing || (!connected && !fromEnv));
  const missingRequired = asked.some((field) => field.required && !values[field.name]?.trim());

  return (
    <div className="rounded-[var(--radius)] border bg-surface-raised">
      <div className="flex flex-wrap items-start justify-between gap-3 px-4 py-3.5">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h3 className="font-medium text-fg">{spec.label}</h3>
            <ConnectionState credential={credential} fromEnv={fromEnv} />
          </div>
          <p className="mt-1 max-w-prose text-sm text-fg-muted">{spec.description}</p>
          {credential ? <Hints credential={credential} /> : null}
          {fromEnv ? (
            <p className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-fg-muted">
              <span>
                Using <code className="font-mono">{spec.env_var}</code> from the environment
              </span>
              {spec.env_last4 ? (
                <span className="font-mono">••••{spec.env_last4}</span>
              ) : null}
              {envVerdict ? (
                <span
                  className={
                    envVerdict === "ok" ? "text-status-succeeded" : "text-status-failed"
                  }
                >
                  {envVerdict === "ok" ? "Working" : "Last test failed"}
                </span>
              ) : null}
            </p>
          ) : null}
        </div>

        {fromEnv && canWrite ? (
          <div className="flex shrink-0 items-center gap-2">
            <Button
              variant="secondary"
              size="sm"
              disabled={testFromEnv.isPending}
              onClick={() => testFromEnv.mutate()}
            >
              {testFromEnv.isPending ? <Spinner label="Testing" /> : <Plug aria-hidden />}
              Test
            </Button>
            <Button variant="secondary" size="sm" onClick={() => setEditing((on) => !on)}>
              {editing ? "Cancel" : "Override for this workspace"}
            </Button>
          </div>
        ) : null}

        {connected && canWrite ? (
          <div className="flex shrink-0 items-center gap-2">
            <Button
              variant="secondary"
              size="sm"
              disabled={test.isPending}
              onClick={() => credential && test.mutate(credential.id)}
            >
              {test.isPending ? <Spinner label="Testing" /> : <Plug aria-hidden />}
              Test
            </Button>
            <Button variant="secondary" size="sm" onClick={() => setEditing((on) => !on)}>
              {editing ? "Cancel" : "Replace"}
            </Button>
            <Button
              variant="ghost"
              size="icon"
              aria-label={`Disconnect ${spec.label}`}
              onClick={() => setConfirmingDelete(true)}
            >
              <Trash2 aria-hidden />
            </Button>
          </div>
        ) : null}
      </div>

      {showForm ? (
        <form
          className="space-y-3 border-t px-4 py-4"
          onSubmit={(event) => {
            event.preventDefault();
            setError(null);
            save.mutate();
          }}
        >
          <div className="grid gap-3 sm:grid-cols-2">
            {asked.map((field) => (
              <Field
                key={field.name}
                label={field.label}
                type={field.secret ? "password" : "text"}
                autoComplete="off"
                value={values[field.name] ?? ""}
                required={field.required}
                hint={field.hint || (field.required ? undefined : "Optional")}
                onChange={(event) =>
                  setValues((current) => ({ ...current, [field.name]: event.target.value }))
                }
              />
            ))}
          </div>
          {error ? <p className="text-xs text-status-failed">{error}</p> : null}
          <div className="flex flex-wrap items-center gap-2">
            {byConsent ? (
              <Button
                type="button"
                size="sm"
                disabled={connect.isPending || missingRequired}
                onClick={() => {
                  setError(null);
                  connect.mutate();
                }}
              >
                {connect.isPending ? <Spinner label="Opening Google" /> : null}
                Continue with Google
              </Button>
            ) : (
              <Button type="submit" size="sm" disabled={save.isPending || missingRequired}>
                {save.isPending ? <Spinner label="Saving" /> : null}
                {connected ? "Replace and test" : "Connect and test"}
              </Button>
            )}
            {spec.oauth_provider && !spec.oauth_ready ? (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => {
                  setError(null);
                  setByHand((on) => !on);
                }}
              >
                {byHand ? "Use Google sign-in" : "Paste all values instead"}
              </Button>
            ) : null}
            {connected ? (
              <Button type="button" variant="ghost" size="sm" onClick={() => setEditing(false)}>
                Cancel
              </Button>
            ) : null}
          </div>
          {byConsent ? (
            <p className="text-xs text-fg-subtle">
              The account owner signs in with their own Google account and approves read access.
              The token is stored here — they never see it, and neither do you.
            </p>
          ) : null}
        </form>
      ) : null}

      {!canWrite ? <p className="border-t px-4 py-3 text-sm text-fg-muted">{reason}</p> : null}

      <ConfirmDialog
        open={confirmingDelete}
        onOpenChange={setConfirmingDelete}
        title={`Disconnect ${spec.label}?`}
        description="The stored key is deleted. It cannot be recovered — you would have to paste it again."
        body={
          <p className="text-sm text-fg-muted">
            Runs will continue without this source and the report will say which sections lost
            evidence because of it.
          </p>
        }
        confirmLabel="Disconnect"
        destructive
        onConfirm={async () => {
          if (credential) await remove.mutateAsync(credential.id);
        }}
      />
    </div>
  );
}

/**
 * Report what came back from a consent round trip, once.
 *
 * The callback redirects here with `?google_ads=connected|error`, so the
 * outcome arrives in the URL rather than in a response. It is read from
 * `window.location` rather than `useSearchParams` so this component does not
 * drag a Suspense boundary onto every screen that renders a card, and the
 * parameters are stripped afterwards so a refresh does not re-announce it.
 */
function useOauthOutcome(spec: CredentialKindInfo, invalidate: () => Promise<void>) {
  const provider = spec.oauth_provider;
  useEffect(() => {
    if (!provider) return;
    const params = new URLSearchParams(window.location.search);
    const outcome = params.get("google_ads");
    if (!outcome) return;

    if (outcome === "connected") {
      toast.success(`${spec.label} connected`, {
        description: params.get("account") ?? undefined,
      });
      void invalidate();
    } else {
      toast.error(`${spec.label} was not connected`, {
        description: params.get("reason") ?? undefined,
      });
    }
    params.delete("google_ads");
    params.delete("account");
    params.delete("reason");
    const query = params.toString();
    window.history.replaceState({}, "", `${window.location.pathname}${query ? `?${query}` : ""}`);
  }, [provider, spec.label, invalidate]);
}

function ConnectionState({
  credential,
  fromEnv,
}: {
  credential: CredentialSummary | undefined;
  fromEnv?: boolean;
}) {
  if (!credential) {
    // "Not connected" next to a source the deployment already configured is
    // simply false: the run would use it. The line below the description names
    // the variable, so the state here says configured and leaves it at that.
    if (fromEnv) {
      return <span className="text-xs text-fg-muted">Configured</span>;
    }
    return <span className="text-xs text-fg-subtle">Not connected</span>;
  }
  if (credential.last_test_ok === true) {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-status-success">
        <CircleCheck className="size-3.5" aria-hidden />
        Working
      </span>
    );
  }
  if (credential.last_test_ok === false) {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-status-failed">
        <CircleX className="size-3.5" aria-hidden />
        Last test failed
      </span>
    );
  }
  return <span className="text-xs text-fg-muted">Stored, never tested</span>;
}

/**
 * One hint, rendered.
 *
 * Not every hint is a scalar: a Google Ads grant records every account it
 * reaches, and `String(list)` would put `[object Object]` on the card.
 */
function hintValue(key: string, value: string | number | boolean | null | AccessibleAccount[]) {
  if (Array.isArray(value)) {
    return value.length === 1 ? "1 account" : `${value.length} accounts`;
  }
  return key === "last4" ? `••••${String(value)}` : String(value);
}

/** The masked hints the vault is allowed to show. Never the secret. */
function Hints({ credential }: { credential: CredentialSummary }) {
  const entries = Object.entries(credential.meta).filter(([, value]) => value !== null);
  return (
    <dl className={cn("mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs")}>
      {entries.map(([key, value]) => (
        <div key={key} className="flex items-center gap-1.5">
          <dt className="text-fg-subtle">{key.replace(/_/g, " ")}</dt>
          <dd className="font-mono text-fg-muted">{hintValue(key, value)}</dd>
        </div>
      ))}
      {credential.last_tested_at ? (
        <div className="flex items-center gap-1.5">
          <dt className="text-fg-subtle">tested</dt>
          <dd className="text-fg-muted">{relativeTime(credential.last_tested_at)}</dd>
        </div>
      ) : null}
    </dl>
  );
}
