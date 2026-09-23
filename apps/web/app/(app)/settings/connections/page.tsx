"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef } from "react";

import { ConnectionCard, StaticSourceCard } from "@/components/settings/connection-card";
import { Alert } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toast";
import { errorMessage, useConnections } from "@/lib/queries";
import { useSession } from "@/lib/session";

/**
 * Everything this workspace talks to, as one grid of cards.
 *
 * One question per card — will this source answer when a run calls it? — and
 * one control to change the answer. Almost nothing on this screen asks for a
 * key, because almost nothing here can hold one: every credential is read from
 * the deployment's environment, and what a workspace owns is the decision to
 * use it.
 *
 * Google Ads is the one source that also needs something only a person can
 * give. Its card sends the browser to Google and Google sends it back here,
 * carrying the outcome in the query string — which is what `useEffect` below
 * turns into a sentence. The parameters are then stripped, because a reloaded
 * page should not re-announce a sign-in that happened ten minutes ago.
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

  useGoogleOutcome();

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
          Only an admin can switch a source on or off, so most of these cards are read-only for
          you. Connecting Google is not one of them — that signs in with your own Google account,
          so anyone here can do it. You can also test any source from its ··· menu.
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

/**
 * Say what Google just did, once, and then clean up after it.
 *
 * The outcome cannot travel in React state: the browser left this app entirely
 * and came back on a fresh document. A query parameter is the only channel a
 * redirect from another origin has, so the API writes the answer into one and
 * this reads it.
 *
 * The ref guards against React's development double-effect and against any
 * re-render that happens before the URL is rewritten — both of which would
 * otherwise show the same toast twice.
 */
function useGoogleOutcome() {
  const params = useSearchParams();
  const router = useRouter();
  const announced = useRef(false);

  useEffect(() => {
    const outcome = params.get("google");
    if (!outcome || announced.current) return;
    announced.current = true;

    if (outcome === "connected") {
      const account = params.get("account");
      toast.success("Google connected", {
        description: account
          ? `Reading ${account}. Change the account on the Google Ads card.`
          : undefined,
      });
    } else {
      toast.error("Google was not connected", {
        description: params.get("reason") ?? "Please try again.",
      });
    }

    // `replace`, not `push`: the sign-in is not a place in the history, and
    // leaving it there means Back re-announces it.
    const rest = new URLSearchParams(params.toString());
    for (const key of ["google", "reason", "account"]) rest.delete(key);
    const query = rest.toString();
    router.replace(query ? `/settings/connections?${query}` : "/settings/connections");
  }, [params, router]);
}
