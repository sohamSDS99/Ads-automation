"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CircleCheck, CircleX, Plug, Trash2 } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Field } from "@/components/ui/field";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import {
  createCredential,
  deleteCredential,
  testCredential,
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
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  const remove = useMutation({
    mutationFn: (id: string) => deleteCredential(id),
    onSuccess: async () => {
      await invalidate();
      toast.success(`${spec.label} disconnected`);
    },
  });

  const connected = Boolean(credential);
  const showForm = canWrite && (editing || !connected);
  const missingRequired = spec.fields.some(
    (field) => field.required && !values[field.name]?.trim(),
  );

  return (
    <div className="rounded-[var(--radius)] border bg-surface-raised">
      <div className="flex flex-wrap items-start justify-between gap-3 px-4 py-3.5">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h3 className="font-medium text-fg">{spec.label}</h3>
            <ConnectionState credential={credential} />
          </div>
          <p className="mt-1 max-w-prose text-sm text-fg-muted">{spec.description}</p>
          {credential ? <Hints credential={credential} /> : null}
        </div>

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
            {spec.fields.map((field) => (
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
          <div className="flex items-center gap-2">
            <Button type="submit" size="sm" disabled={save.isPending || missingRequired}>
              {save.isPending ? <Spinner label="Saving" /> : null}
              {connected ? "Replace and test" : "Connect and test"}
            </Button>
            {connected ? (
              <Button type="button" variant="ghost" size="sm" onClick={() => setEditing(false)}>
                Cancel
              </Button>
            ) : null}
          </div>
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

function ConnectionState({ credential }: { credential: CredentialSummary | undefined }) {
  if (!credential) {
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

/** The masked hints the vault is allowed to show. Never the secret. */
function Hints({ credential }: { credential: CredentialSummary }) {
  const entries = Object.entries(credential.meta).filter(([, value]) => value !== null);
  return (
    <dl className={cn("mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs")}>
      {entries.map(([key, value]) => (
        <div key={key} className="flex items-center gap-1.5">
          <dt className="text-fg-subtle">{key.replace(/_/g, " ")}</dt>
          <dd className="font-mono text-fg-muted">
            {key === "last4" ? `••••${String(value)}` : String(value)}
          </dd>
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
