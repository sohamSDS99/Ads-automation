"use client";

import { ConnectionCard, StaticSourceCard } from "@/components/settings/connection-card";
import { Alert } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessage, useConnections } from "@/lib/queries";
import { useSession } from "@/lib/session";

/**
 * Everything this workspace talks to, as one grid of cards.
 *
 * One question per card — will this source answer when a run calls it? — and
 * one control to change the answer. Nothing on this screen asks for a key,
 * because nothing here can hold one: every credential is read from the
 * deployment's environment, and what a workspace owns is the decision to use it.
 *
 * The list is not spelled out here. It comes from `GET /connections`, which
 * reports every source this build knows about, so adding one to the backend
 * adds its card without a change on this screen.
 */
export default function ConnectionsPage() {
  const { has } = useSession();
  const connections = useConnections();
  const error = errorMessage(connections);
  const canWrite = has("credential_write");

  const sources = connections.data?.sources ?? [];
  // The model key first: every research node is a call through it, so it is the
  // one connection whose absence stops a run rather than thinning a report.
  const ordered = [...sources].sort(
    (a, b) => Number(b.required_for_runs) - Number(a.required_for_runs),
  );

  return (
    <div className="flex flex-col gap-5">
      {error ? (
        <Alert tone="error" title="Connections could not be loaded">
          {error}
        </Alert>
      ) : null}

      <p className="max-w-prose text-sm text-fg-muted">
        Every key lives in the deployment&rsquo;s environment, so connecting a source is one click
        and asks for nothing. Only OpenRouter is required — without it a run cannot start; without
        any of the others it runs anyway and the report names the sections that went without
        evidence. It never fills the gap with a guess.
      </p>

      {canWrite ? null : (
        <p className="max-w-prose text-sm text-fg-muted">
          Only an admin can switch a source on or off, so these cards are read-only for you. You
          can still test any of them from its ··· menu, and nothing else in setup is waiting on
          this screen.
        </p>
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {connections.isPending ? (
          <>
            <Skeleton className="h-64 w-full" />
            <Skeleton className="h-64 w-full" />
            <Skeleton className="h-64 w-full" />
          </>
        ) : null}

        {ordered.map((source) => (
          <ConnectionCard key={source.kind} source={source} canWrite={canWrite} />
        ))}

        {connections.isPending ? null : (
          <>
            <StaticSourceCard
              icon="archive"
              label="Ads Transparency Center"
              description="Competitor and own live ads, read from Google's public archive with a browser. No credential, and nothing behind a login."
            />
            <StaticSourceCard
              icon="crawl"
              label="Your own site"
              description="Landing pages, CTAs, forms and page-speed signals, crawled from the site URL on the Business context tab."
            />
          </>
        )}
      </div>
    </div>
  );
}
