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
} from "lucide-react";
import { type ComponentType } from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import {
  connectSource,
  disconnectSource,
  testSource,
  type AccessibleAccount,
  type Source,
  type SourceKind,
} from "@/lib/api/connections";
import { relativeTime } from "@/lib/format";
import { keys } from "@/lib/queries";
import { cn } from "@/lib/utils";

/** One icon per source, so a card is recognisable before it is read. */
const ICONS: Partial<Record<SourceKind, ComponentType<{ className?: string }>>> = {
  google_ads: Megaphone,
  dataforseo: Search,
  webshare: Network,
  openrouter: Sparkles,
};

/**
 * One source, as a card you can take in at a glance and act on in one click.
 *
 * There is nothing to type here and nowhere to type it. A key belongs to the
 * deployment — it is in the environment, the same in every workspace, and the
 * one thing a workspace decides is whether it may be spent. So the card is four
 * lines in a fixed order: what it is, whether it will answer, what it gives the
 * research, and the single switch.
 *
 * Which makes three states, and the card says which one plainly rather than
 * leaving them to be inferred from a greyed-out button:
 *
 *   not set up   the deployment supplies no key — and the card names the exact
 *                variables to add, because "not configured" without the list is
 *                a dead end for whoever reads it
 *   ready        the key is there; nobody has switched it on yet
 *   connected    switched on, with the verdict of the last real call
 *
 * Connecting proves itself, so the button is not a promise: the API tests the
 * key on the way through and the toast reports what the upstream actually said.
 */
export function ConnectionCard({ source, canWrite }: { source: Source; canWrite: boolean }) {
  const queryClient = useQueryClient();
  const invalidate = () => queryClient.invalidateQueries({ queryKey: keys.connections });

  const connect = useMutation({
    mutationFn: () => connectSource(source.kind),
    onSuccess: async (updated) => {
      await invalidate();
      // One act, one message. Connecting runs the test, so saying "connected"
      // and then leaving the verdict to a pill the eye has already left is how
      // a source ends up switched on and quietly broken.
      if (updated.last_test_ok === false) {
        toast.error(`${source.label} is connected but not answering`, {
          description: updated.last_test_detail ?? undefined,
        });
      } else {
        toast.success(`${source.label} connected`, {
          description: updated.last_test_detail ?? undefined,
        });
      }
    },
    onError: (error) =>
      toast.error(`${source.label} was not connected`, {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      }),
  });

  const disconnect = useMutation({
    mutationFn: () => disconnectSource(source.kind),
    onSuccess: async () => {
      await invalidate();
      // No confirmation step before this, on purpose. Disconnecting destroys
      // nothing — the key is in the deployment's environment and reconnecting
      // is the same one click — so a dialog asking "are you sure" would be
      // guarding against an action that undoes itself.
      toast.success(`${source.label} disconnected`, {
        description: "Runs continue without it. Connect again whenever you need it.",
      });
    },
    onError: (error) =>
      toast.error("Not disconnected", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      }),
  });

  const test = useMutation({
    mutationFn: () => testSource(source.kind),
    onSuccess: async (result) => {
      await invalidate();
      if (result.ok) toast.success(`${source.label} is working`, { description: result.detail });
      else toast.error(`${source.label} did not answer`, { description: result.detail });
    },
    onError: (error) =>
      toast.error("The test could not run", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      }),
  });

  const busy = connect.isPending || disconnect.isPending;

  return (
    <article className="flex flex-col rounded-[var(--radius)] border bg-surface-raised p-5">
      <div className="flex items-start justify-between gap-2">
        <h3 className="flex min-w-0 items-center gap-2.5 text-[length:var(--text-md)] font-semibold tracking-tight text-fg">
          <SourceIcon kind={source.kind} />
          <span className="truncate">{source.label}</span>
        </h3>

        <DropdownMenu>
          <DropdownMenuTrigger
            aria-label={`More for ${source.label}`}
            className="-mr-1.5 -mt-1 shrink-0 rounded-[calc(var(--radius)-4px)] p-1.5 text-fg-subtle transition-colors hover:bg-surface-hover hover:text-fg"
          >
            <MoreHorizontal className="size-4" aria-hidden />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {/* Testing needs no permission and changes nothing. The person
                watching a run skip a source is often not the person who can
                switch it back on, and telling them why is the point. */}
            <DropdownMenuItem
              disabled={test.isPending || !source.configured}
              onSelect={() => test.mutate()}
            >
              <Plug aria-hidden />
              {test.isPending ? "Testing…" : "Test connection"}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <div className="mt-3">
        <StatePill source={source} />
      </div>

      <p className="mt-3 text-sm text-fg-muted">{source.description}</p>

      <div className="mt-auto pt-5">
        <Detail source={source} />

        {/* No button without the permission, and no apology on every card
            either: the screen says once, above the grid, who can switch a
            source. Six cards each repeating it is six times the words for one
            fact. */}
        {canWrite ? (
          <Button
            /* Primary only when pressing it would do something. A disabled
               primary still reads as the next action and pulls the eye away
               from the line above it, which on an unconfigured card is the
               only thing that moves this forward. */
            variant={source.connected || !source.configured ? "secondary" : "primary"}
            className="w-full"
            disabled={busy || !source.configured}
            onClick={() => (source.connected ? disconnect.mutate() : connect.mutate())}
          >
            {busy ? <Spinner label={source.connected ? "Disconnecting" : "Connecting"} /> : null}
            {source.connected ? "Disconnect" : "Connect"}
          </Button>
        ) : null}
      </div>
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

function SourceIcon({ kind }: { kind: SourceKind }) {
  const Icon = ICONS[kind] ?? KeyRound;
  return <Icon className="size-5 shrink-0 text-fg" />;
}

/** Whether this source will answer when a run calls it, in three words or less. */
function StatePill({ source }: { source: Source }) {
  if (!source.configured) return <Pill tone="off" label="Not set up" />;
  if (!source.connected) return <Pill tone="env" label="Ready to connect" />;
  if (source.last_test_ok === true) return <Pill tone="ok" label="Working" />;
  if (source.last_test_ok === false) return <Pill tone="bad" label="Not working" />;
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
 * The two lines above the button, and the only place a variable name appears.
 *
 * Fixed height so a grid of cards keeps its buttons on one line whatever each
 * card has to say. An unconfigured source spends it on the exact variables to
 * set, because that is the entire instruction — a card that says "not
 * configured" and stops has handed the reader a search rather than a task.
 */
function Detail({ source }: { source: Source }) {
  if (!source.configured) {
    return (
      <div className="mb-3 min-h-14 text-xs leading-5">
        <p className="text-fg-muted">Set these on the deployment, then reload:</p>
        <p className="font-mono break-words text-fg-subtle">{source.missing_env_vars.join(" · ")}</p>
      </div>
    );
  }

  const identity = source.connected ? identify(source.meta) : null;
  const tested = source.last_tested_at
    ? `tested ${relativeTime(source.last_tested_at)}`
    : "never tested";

  return (
    <p className="mb-3 min-h-14 font-mono text-xs leading-5 break-words text-fg-subtle">
      {source.env_vars.join(" · ")}
      <br />
      {source.connected ? (
        <>
          {identity ? `${identity} · ` : ""}
          {tested}
        </>
      ) : (
        "set on the deployment · not switched on yet"
      )}
    </p>
  );
}

/** The most specific masked hint a test recorded. Never the secret. */
function identify(meta: Source["meta"]): string | null {
  const accessible = meta.accessible;
  if (Array.isArray(accessible) && accessible.length) {
    const named = accessible.find((account: AccessibleAccount) => !account.manager) ?? accessible[0];
    const extra = accessible.length - 1;
    const name = named?.name ?? named?.customer_id ?? "";
    return extra > 0 ? `${name} +${extra} more` : name;
  }
  if (meta.account_name) return String(meta.account_name);
  if (meta.customer_id) return String(meta.customer_id);
  if (meta.login) return String(meta.login);
  if (meta.last4) return `••••${String(meta.last4)}`;
  return null;
}
