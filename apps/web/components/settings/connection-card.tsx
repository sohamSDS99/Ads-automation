"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Globe,
  KeyRound,
  Megaphone,
  MoreHorizontal,
  Network,
  Plug,
  RefreshCw,
  ScanSearch,
  Search,
  Sparkles,
} from "lucide-react";
import { type ComponentType } from "react";

import { GoogleMark } from "@/components/settings/google-mark";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Select } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import {
  authorizeGoogle,
  chooseAccount,
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
 * Google Ads adds a fourth, because two of its five values are not the
 * deployment's to hold: **sign-in needed**. The deployment has the developer
 * token and the OAuth client, and what is missing is a person — any person in
 * the workspace — pressing Connect with Google. That state is not "not set up"
 * and it is not "ready": it has its own fix, and its own button, and it is the
 * one button on this screen that does not need an admin, because what it hands
 * over is the presser's own Google account.
 *
 * Connecting proves itself, so the button is not a promise: the API tests the
 * key on the way through and the toast reports what the upstream actually said.
 */
export function ConnectionCard({ source, canWrite }: { source: Source; canWrite: boolean }) {
  const queryClient = useQueryClient();
  const invalidate = () => queryClient.invalidateQueries({ queryKey: keys.connections });
  const oauth = source.oauth;

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

  const signIn = useMutation({
    // Where Google's redirect should land: the screen this card is on, read at
    // the moment of the click rather than through `useSearchParams`. The value
    // is only ever needed here, and the hook would opt this component — and so
    // every page rendering a card — into a Suspense boundary for it.
    mutationFn: () => authorizeGoogle(window.location.pathname + window.location.search),
    // No `onSuccess` invalidation and no toast: this navigates away. The
    // outcome arrives back as `?google=…` on the returning request, which the
    // page reads — a toast fired here would be destroyed by the navigation it
    // is announcing.
    onSuccess: ({ url }) => window.location.assign(url),
    onError: (error) =>
      toast.error("Google sign-in could not be started", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      }),
  });

  const switchAccount = useMutation({
    mutationFn: (customerId: string) => chooseAccount(source.kind, customerId),
    onSuccess: async (updated) => {
      await invalidate();
      if (updated.last_test_ok === false) {
        toast.error("That account did not answer", {
          description: updated.last_test_detail ?? undefined,
        });
      } else {
        toast.success("Account changed", { description: updated.last_test_detail ?? undefined });
      }
    },
    onError: (error) =>
      toast.error("The account was not changed", {
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
        description: oauth
          ? "The Google sign-in has been handed back. Connect again whenever you need it."
          : "Runs continue without it. Connect again whenever you need it.",
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

  const busy = connect.isPending || disconnect.isPending || signIn.isPending;
  const needsSignIn = oauth !== null && !oauth.granted;

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
              disabled={test.isPending || !source.configured || needsSignIn}
              onSelect={() => test.mutate()}
            >
              <Plug aria-hidden />
              {test.isPending ? "Testing…" : "Test connection"}
            </DropdownMenuItem>
            {/* Signing in again is how a revoked grant, an expired one, or a
                colleague's account that should have been yours gets replaced.
                No permission, same as the button: it is the presser's own
                Google account either way. */}
            {oauth && oauth.granted ? (
              <DropdownMenuItem disabled={signIn.isPending} onSelect={() => signIn.mutate()}>
                <RefreshCw aria-hidden />
                {signIn.isPending ? "Opening Google…" : "Sign in with a different account"}
              </DropdownMenuItem>
            ) : null}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <div className="mt-3">
        <StatePill source={source} />
      </div>

      <p className="mt-3 text-sm text-fg-muted">{source.description}</p>

      {/* Only shown when the choice is real. One account is not a decision, and
          a picker offering it would imply there was something to get wrong. */}
      {oauth && oauth.granted && oauth.accounts.length > 1 ? (
        <label className="mt-4 block text-xs text-fg-muted">
          Account the research reads
          <Select
            className="mt-1.5"
            value={oauth.customer_id ?? ""}
            disabled={switchAccount.isPending}
            onChange={(event) => switchAccount.mutate(event.target.value)}
          >
            {oauth.accounts.map((account) => (
              <option key={account.customer_id} value={account.customer_id}>
                {accountLabel(account)}
              </option>
            ))}
          </Select>
        </label>
      ) : null}

      <div className="mt-auto pt-5">
        <Detail source={source} />

        {needsSignIn ? (
          /* Not gated on `canWrite`, and this is the whole point of the change:
             what this button hands over is the presser's own Google account,
             and the developer token it is joined to belongs to the deployment.
             Requiring an admin here would mean one person minting refresh
             tokens on a laptop for everybody else, which is the arrangement
             that left this source unconnected. */
          <Button
            variant={source.configured ? "primary" : "secondary"}
            className="w-full"
            disabled={busy || !source.configured}
            onClick={() => signIn.mutate()}
          >
            {signIn.isPending ? (
              <Spinner label="Opening Google" />
            ) : (
              <GoogleMark className="size-4 shrink-0" />
            )}
            {oauth.action}
          </Button>
        ) : canWrite ? (
          <Button
            /* Primary only when pressing it would do something. A disabled
               primary still reads as the next action and pulls the eye away
               from the line above it, which on an unconfigured card is the
               only thing that moves this forward. */
            variant={source.connected || !source.configured ? "secondary" : "primary"}
            className="w-full"
            /* Only *connecting* needs a key. Switching a source off never does,
               and disabling Disconnect here stranded the one state this
               product actually shipped into: migration 0012 carries every
               previously-stored credential forward as a connection, so a
               deployment that has not yet moved its keys into the environment
               opens this screen connected and unconfigured — with no way to
               act on either fact. */
            disabled={busy || (!source.connected && !source.configured)}
            onClick={() => (source.connected ? disconnect.mutate() : connect.mutate())}
          >
            {busy ? <Spinner label={source.connected ? "Disconnecting" : "Connecting"} /> : null}
            {source.connected ? "Disconnect" : "Connect"}
          </Button>
        ) : null}
        {/* No button without the permission, and no apology on every card
            either: the screen says once, above the grid, who can switch a
            source. Six cards each repeating it is six times the words for one
            fact. */}
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
  // Configured and connected are two facts, so four states, and the pair that
  // are both false read very differently from the pair that disagree. "Not set
  // up" means nobody has done anything; "Connected, no key" means somebody
  // switched this on and the deployment then had nothing to answer with — a
  // run skips it, and the reader needs to know that is not the same thing.
  if (!source.configured) {
    return source.connected ? (
      <Pill tone="bad" label="Connected, no key" />
    ) : (
      <Pill tone="off" label="Not set up" />
    );
  }
  // A third fact for an OAuth source, and it outranks the other two: without a
  // grant there is no credential, whatever the row says. "Ready to connect"
  // here would point at the wrong button.
  if (source.oauth && !source.oauth.granted) return <Pill tone="env" label="Sign-in needed" />;
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

  // An OAuth source spends these two lines on the person rather than on the
  // plumbing. Whose account this is, is the first question asked when a grant
  // stops working, and no variable name answers it.
  const oauth = source.oauth;
  if (oauth && !oauth.granted) {
    return <p className="mb-3 min-h-14 text-xs leading-5 text-fg-muted">{oauth.explains}</p>;
  }
  if (oauth) {
    const account = oauth.accounts.find((row) => row.customer_id === oauth.customer_id);
    return (
      <p className="mb-3 min-h-14 text-xs leading-5 break-words text-fg-subtle">
        {oauth.email ? <>Signed in as {oauth.email}</> : <>Signed in to Google</>}
        {oauth.granted_by_name ? ` by ${oauth.granted_by_name}` : ""}
        <br />
        {account ? accountLabel(account) : (oauth.customer_id ?? "no account chosen")}
        {source.last_tested_at ? ` · tested ${relativeTime(source.last_tested_at)}` : ""}
      </p>
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

/** An account as a person would name it: what it is called, then which one it is. */
function accountLabel(account: AccessibleAccount): string {
  const id = formatCustomerId(account.customer_id);
  const suffix = account.manager ? " · manager" : "";
  return account.name ? `${account.name} (${id})${suffix}` : `${id}${suffix}`;
}

/** Google prints customer ids in threes. Ten digits in a row is a serial number. */
function formatCustomerId(customerId: string): string {
  return /^\d{10}$/.test(customerId)
    ? `${customerId.slice(0, 3)}-${customerId.slice(3, 6)}-${customerId.slice(6)}`
    : customerId;
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
