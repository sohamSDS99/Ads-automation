"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Globe,
  KeyRound,
  Megaphone,
  MoreHorizontal,
  Network,
  Plug,
  ScanSearch,
  Search,
  Sparkles,
  Trash2,
} from "lucide-react";
import { useState, type ComponentType } from "react";

import { ConnectDialog } from "@/components/settings/connect-dialog";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import {
  deleteCredential,
  testCredential,
  testCredentialKind,
  type AccessibleAccount,
  type CredentialKind,
  type CredentialKindInfo,
  type CredentialScope,
  type CredentialSummary,
} from "@/lib/api/credentials";
import { relativeTime } from "@/lib/format";
import { keys } from "@/lib/queries";
import { cn } from "@/lib/utils";

/** One icon per source, so a card is recognisable before it is read. */
const ICONS: Partial<Record<CredentialKind, ComponentType<{ className?: string }>>> = {
  google_ads: Megaphone,
  dataforseo: Search,
  webshare: Network,
  openrouter: Sparkles,
};

/**
 * One source, as a card you can take in at a glance.
 *
 * The whole card is four lines in a fixed order — what it is, whether it works,
 * what it gives the research, and the one thing you might want to do about it.
 * Nothing expands in place: connecting and replacing both happen in a dialog,
 * so a grid of these never reflows under the pointer and the card you were
 * reading does not move while you read it.
 *
 * Testing and disconnecting are real but rare, so they are behind the overflow
 * menu rather than competing with the button that most visits are here for.
 */
export function ConnectionCard({
  spec,
  credential,
  canWrite,
  scope = "workspace",
  description,
}: {
  spec: CredentialKindInfo;
  credential: CredentialSummary | undefined;
  /** Whether this person may store a key. Testing only needs `read`. */
  canWrite: boolean;
  /** `user` is the personal override on the account screen. */
  scope?: CredentialScope;
  /** Replaces the kind's own blurb where the surrounding screen needs to. */
  description?: string;
}) {
  const queryClient = useQueryClient();
  const [connecting, setConnecting] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  /**
   * A stored credential records its own verdict. A key that came from the
   * deployment's environment has no row to write one back to, so its verdict
   * lives for as long as this screen does and no longer.
   */
  const [envVerdict, setEnvVerdict] = useState<"ok" | "failed" | null>(null);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: keys.credentials });

  const test = useMutation({
    mutationFn: async () => {
      if (credential) return testCredential(credential.id);
      const result = await testCredentialKind(spec.kind);
      setEnvVerdict(result.ok ? "ok" : "failed");
      return result;
    },
    onSuccess: async (result) => {
      await invalidate();
      if (result.ok) toast.success(`${spec.label} is working`, { description: result.detail });
      else toast.error(`${spec.label} did not answer`, { description: result.detail });
    },
    onError: (error) =>
      toast.error("The test could not run", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      }),
  });

  const remove = useMutation({
    mutationFn: (id: string) => deleteCredential(id),
    onSuccess: async () => {
      await invalidate();
      toast.success(`${spec.label} disconnected`);
    },
    onError: (error) =>
      toast.error("Not disconnected", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      }),
  });

  const connected = Boolean(credential);
  /**
   * This source already works with nothing stored, because the deployment put
   * its key in a variable and no row overrides it. A stored credential still
   * wins — `resolve_secret` looks in the vault first — so this is only ever the
   * state of a card with no row behind it.
   *
   * Workspace scope only. A variable on the deployment is a default for the
   * whole workspace and never anybody's personal key, so the account screen's
   * card would otherwise report "Configured" for a key its owner never set.
   */
  const fromEnv = scope === "workspace" && !connected && Boolean(spec.env_configured);
  const byConsent = Boolean(spec.oauth_provider) && spec.oauth_ready;

  return (
    <article className="flex flex-col rounded-[var(--radius)] border bg-surface-raised p-5">
      <div className="flex items-start justify-between gap-2">
        <h3 className="flex min-w-0 items-center gap-2.5 text-[length:var(--text-md)] font-semibold tracking-tight text-fg">
          <SourceIcon kind={spec.kind} />
          <span className="truncate">{spec.label}</span>
        </h3>

        <DropdownMenu>
          <DropdownMenuTrigger
            aria-label={`More for ${spec.label}`}
            className="-mr-1.5 -mt-1 shrink-0 rounded-[calc(var(--radius)-4px)] p-1.5 text-fg-subtle transition-colors hover:bg-surface-hover hover:text-fg"
          >
            <MoreHorizontal className="size-4" aria-hidden />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem disabled={test.isPending} onSelect={() => test.mutate()}>
              <Plug aria-hidden />
              {test.isPending ? "Testing…" : "Test connection"}
            </DropdownMenuItem>
            {connected && canWrite ? (
              <DropdownMenuItem
                className="text-status-failed [&_svg]:text-status-failed"
                onSelect={() => setConfirmingDelete(true)}
              >
                <Trash2 aria-hidden />
                Disconnect
              </DropdownMenuItem>
            ) : null}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <div className="mt-3">
        <StatePill credential={credential} fromEnv={fromEnv} envVerdict={envVerdict} />
      </div>

      <p className="mt-3 text-sm text-fg-muted">{description ?? spec.description}</p>

      <div className="mt-auto pt-5">
        <p className="mb-3 min-h-8 font-mono text-xs leading-5 text-fg-subtle">
          {credential ? <StoredMeta credential={credential} /> : null}
          {fromEnv ? (
            <>
              {spec.env_var}
              {spec.env_last4 ? ` · ••••${spec.env_last4}` : ""}
              <br />
              set on the deployment, not here
            </>
          ) : null}
        </p>

        {/* No button without the permission, and no apology on every card
            either: the screen says once, above the grid, who can store a key.
            Six cards each repeating it is six times the words for one fact. */}
        {canWrite ? (
          <Button
            variant={connected || fromEnv ? "secondary" : "primary"}
            className="w-full"
            onClick={() => setConnecting(true)}
          >
            {connected || fromEnv
              ? byConsent
                ? "Reconnect Google account"
                : "Replace key"
              : byConsent
                ? "Connect Google account"
                : "Add key"}
          </Button>
        ) : null}
      </div>

      <ConnectDialog
        spec={spec}
        scope={scope}
        replacing={connected || fromEnv}
        open={connecting}
        onOpenChange={setConnecting}
      />

      <ConfirmDialog
        open={confirmingDelete}
        onOpenChange={setConfirmingDelete}
        title={`Disconnect ${spec.label}?`}
        description="The stored key is deleted. It cannot be recovered — you would have to paste it again."
        body={
          <p className="text-sm text-fg-muted">
            Runs continue without this source, and the report says which sections lost evidence
            because of it.
          </p>
        }
        confirmLabel="Disconnect"
        destructive
        onConfirm={async () => {
          if (credential) await remove.mutateAsync(credential.id);
        }}
      />
    </article>
  );
}

/**
 * A source that needs no configuration at all, in the same card shape.
 *
 * Deliberately not a different kind of row. These used to be a bulleted list
 * under a second heading, which said they were a lesser sort of source — they
 * are not, they are the two that cannot be misconfigured.
 */
export function StaticSourceCard({
  icon,
  label,
  description,
}: {
  icon: "crawl" | "archive";
  label: string;
  description: string;
}) {
  const Icon = icon === "archive" ? ScanSearch : Globe;
  return (
    <article className="flex flex-col rounded-[var(--radius)] border border-dashed bg-surface p-5">
      <h3 className="flex items-center gap-2.5 text-[length:var(--text-md)] font-semibold tracking-tight text-fg">
        <Icon className="size-5 shrink-0 text-fg-subtle" aria-hidden />
        <span className="truncate">{label}</span>
      </h3>
      <div className="mt-3">
        <Pill tone="off" label="No setup needed" />
      </div>
      <p className="mt-3 text-sm text-fg-muted">{description}</p>
    </article>
  );
}

function SourceIcon({ kind }: { kind: CredentialKind }) {
  const Icon = ICONS[kind] ?? KeyRound;
  return <Icon className="size-5 shrink-0 text-fg" />;
}

/** Whether this source will answer when a run calls it, in three words or less. */
function StatePill({
  credential,
  fromEnv,
  envVerdict,
}: {
  credential: CredentialSummary | undefined;
  fromEnv: boolean;
  envVerdict: "ok" | "failed" | null;
}) {
  if (!credential) {
    // "Not connected" next to a source the deployment already configured is
    // simply false — a run would use it. The meta line below names the
    // variable, so the pill says configured and leaves it there.
    if (!fromEnv) return <Pill tone="off" label="Not connected" />;
    if (envVerdict === "ok") return <Pill tone="ok" label="Working" />;
    if (envVerdict === "failed") return <Pill tone="bad" label="Not working" />;
    return <Pill tone="env" label="Configured" />;
  }
  if (credential.last_test_ok === true) return <Pill tone="ok" label="Connected" />;
  if (credential.last_test_ok === false) return <Pill tone="bad" label="Not working" />;
  return <Pill tone="env" label="Connected, untested" />;
}

const TONES = {
  ok: { fill: "bg-status-success/15", dot: "bg-status-success" },
  env: { fill: "bg-status-gate/15", dot: "bg-status-gate" },
  bad: { fill: "bg-status-failed/15", dot: "bg-status-failed" },
  off: { fill: "bg-surface-hover", dot: "bg-status-skipped" },
} as const;

/**
 * The colour is the tint and the dot; the word carries the meaning. A pill
 * that only differed by hue would say nothing to someone who cannot separate
 * amber from green, which is the same rule `StatusPill` follows.
 */
function Pill({ tone, label }: { tone: keyof typeof TONES; label: string }) {
  const { fill, dot } = TONES[tone];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium text-fg",
        fill,
      )}
    >
      <span aria-hidden className={cn("size-1.5 rounded-full", dot)} />
      {label}
    </span>
  );
}

/**
 * Who this key belongs to and when it was last proven — the two facts about a
 * stored credential that are safe to show and worth showing.
 *
 * Not every hint is a scalar: a Google Ads grant records every account it
 * reaches, and `String(list)` would put `[object Object]` on the card.
 */
function StoredMeta({ credential }: { credential: CredentialSummary }) {
  const identity = identify(credential.meta);
  const tested = credential.last_tested_at
    ? `tested ${relativeTime(credential.last_tested_at)}`
    : "never tested";
  return (
    <>
      {identity ? `${identity} · ` : ""}
      {tested}
    </>
  );
}

/** The most specific masked hint the vault will show. Never the secret. */
function identify(meta: CredentialSummary["meta"]): string | null {
  const accessible = meta.accessible;
  if (Array.isArray(accessible) && accessible.length) {
    const named = accessible.find((account: AccessibleAccount) => !account.manager) ?? accessible[0];
    const extra = accessible.length - 1;
    const name = named?.name ?? named?.customer_id ?? "";
    return extra > 0 ? `${name} +${extra} more` : name;
  }
  if (meta.customer_id) return String(meta.customer_id);
  if (meta.login) return String(meta.login);
  if (meta.last4) return `••••${String(meta.last4)}`;
  return null;
}
