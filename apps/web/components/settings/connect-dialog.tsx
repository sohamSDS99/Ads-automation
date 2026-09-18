"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import {
  createCredential,
  startGoogleAdsOauth,
  testCredential,
  type CredentialKindInfo,
  type CredentialScope,
} from "@/lib/api/credentials";
import { keys } from "@/lib/queries";

/**
 * Where a key is actually typed.
 *
 * A dialog rather than a panel that unfolds inside the card: a grid of cards
 * that grow one at a time reflows the whole row, and the card you clicked ends
 * up somewhere else on the screen. This also lets the card stay four lines
 * long no matter how many fields the source needs — Google Ads has six.
 *
 * There is no edit, only replace. A credential is write-only, so a half-typed
 * replacement can never leave the old key partly overwritten.
 */
export function ConnectDialog({
  spec,
  scope,
  replacing,
  open,
  onOpenChange,
}: {
  spec: CredentialKindInfo;
  scope: CredentialScope;
  /** Whether something already supplies this source, stored or from the environment. */
  replacing: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [values, setValues] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);

  // A dialog that remembers half a key from last time is a dialog that can
  // submit one. Opening it is always a fresh start.
  useEffect(() => {
    if (open) {
      setValues({});
      setError(null);
    }
  }, [open]);

  /**
   * Whether this source is connected by consent rather than by typing.
   *
   * Read off the kind, not hardcoded: the API says which kinds have a provider,
   * whether the deployment configured a client for it, and which of their
   * fields that provider supplies. A second OAuth source needs no change here.
   */
  const byConsent = Boolean(spec.oauth_provider) && spec.oauth_ready;
  // Consent supplies five of Google Ads' six values, so asking for them
  // alongside it is offering a worse way to do the same thing. Where no client
  // is configured, consent cannot run at all and the full form is the only way.
  const asked = byConsent
    ? spec.fields.filter((field) => !spec.oauth_fields.includes(field.name))
    : spec.fields;
  const missingRequired = asked.some((field) => field.required && !values[field.name]?.trim());

  const test = useMutation({
    mutationFn: (id: string) => testCredential(id),
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: keys.credentials });
      if (result.ok) toast.success(`${spec.label} is working`, { description: result.detail });
      else toast.error(`${spec.label} did not answer`, { description: result.detail });
    },
  });

  const save = useMutation({
    mutationFn: () => createCredential({ kind: spec.kind, scope, values }),
    onSuccess: async (stored) => {
      onOpenChange(false);
      await queryClient.invalidateQueries({ queryKey: keys.credentials });
      toast.success(`${spec.label} connected`, { description: "Testing it now." });
      test.mutate(stored.id);
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.detail : "The credential could not be stored."),
  });

  const connect = useMutation({
    mutationFn: () =>
      startGoogleAdsOauth({
        developer_token: values.developer_token ?? "",
        // Come back to this screen, not to a default one: landing somewhere
        // else after consent reads as having lost your place.
        return_to: `${window.location.pathname}${window.location.search}`,
      }),
    // A full navigation, not a popup: Google refuses to render consent in an
    // iframe, and a popup is the thing browsers block.
    onSuccess: (started) => window.location.assign(started.url),
    onError: (err) =>
      setError(err instanceof ApiError ? err.detail : "Google could not be reached."),
  });

  const busy = save.isPending || connect.isPending;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        title={replacing ? `Replace ${spec.label}` : `Connect ${spec.label}`}
        description={
          byConsent
            ? "Sign in with the Google account that owns the ads data. The token is stored here — nobody sees it again, including you."
            : replacing
              ? "The new key replaces the old one as soon as you save. The old one is not recoverable."
              : spec.description
        }
      >
        <form
          onSubmit={(event) => {
            event.preventDefault();
            setError(null);
            if (byConsent) connect.mutate();
            else save.mutate();
          }}
        >
          <DialogBody>
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

            {spec.oauth_provider && !spec.oauth_ready ? (
              <p className="text-xs text-fg-muted">
                This deployment has no Google OAuth client configured, so signing in is not
                available here. Every value above can be minted with{" "}
                <code className="font-mono">scripts/google-ads-oauth.py</code>.
              </p>
            ) : null}

            {error ? <p className="text-xs text-status-failed">{error}</p> : null}
          </DialogBody>

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={busy || missingRequired}>
              {busy ? <Spinner label={byConsent ? "Opening Google" : "Saving"} /> : null}
              {byConsent ? "Continue with Google" : replacing ? "Replace and test" : "Connect and test"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
