"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { ConnectionCard, StaticSourceCard } from "@/components/settings/connection-card";
import { Alert } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toast";
import { credentialFor } from "@/lib/api/credentials";
import { keys, errorMessage, useCredentials } from "@/lib/queries";
import { useSession } from "@/lib/session";

/**
 * Everything this workspace talks to, as one grid of cards.
 *
 * There used to be three shapes on this screen — keyed sources got a card each,
 * the two that need nothing were a bulleted list under a second heading, and
 * the model key lived on a different tab entirely. Three shapes for one
 * question ("will this source answer when a run calls it?") is what made it
 * hard to read. One card shape answers it for all of them, and the two that
 * cannot be misconfigured say so rather than being hidden away.
 *
 * The list is not spelled out here. It comes from `GET /credentials`, which
 * reports every kind this build knows about, so adding a source to the backend
 * adds its card without a change on this screen.
 */
export default function ConnectionsPage() {
  const { has } = useSession();
  const credentials = useCredentials();
  const error = errorMessage(credentials);
  const canWrite = has("credential_write");

  useOauthOutcome();

  const kinds = credentials.data?.kinds ?? [];
  // The model key first: every research node is a call through it, so it is the
  // one connection whose absence stops a run rather than thinning a report.
  const ordered = [...kinds].sort(
    (a, b) => Number(b.kind === "openrouter") - Number(a.kind === "openrouter"),
  );

  return (
    <div className="flex flex-col gap-5">
      {error ? (
        <Alert tone="error" title="Connections could not be loaded">
          {error}
        </Alert>
      ) : null}

      <p className="max-w-prose text-sm text-fg-muted">
        Every source here is optional except the model key. Without OpenRouter a run cannot start;
        without any of the others it runs anyway and the report names the sections that went
        without evidence — it never fills the gap with a guess.
      </p>

      {canWrite ? null : (
        <p className="max-w-prose text-sm text-fg-muted">
          Only an admin can store keys, so these cards are read-only for you. You can still test
          any of them from its ··· menu, and nothing else in setup is waiting on this screen.
        </p>
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {credentials.isPending ? (
          <>
            <Skeleton className="h-64 w-full" />
            <Skeleton className="h-64 w-full" />
            <Skeleton className="h-64 w-full" />
          </>
        ) : null}

        {ordered.map((spec) => (
          <ConnectionCard
            key={spec.kind}
            spec={spec}
            credential={credentialFor(credentials.data?.credentials ?? [], spec.kind)}
            canWrite={canWrite}
          />
        ))}

        {credentials.isPending ? null : (
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
 * Report what came back from a Google consent round trip, once.
 *
 * The callback redirects here with `?google_ads=connected|error`, so the
 * outcome arrives in the URL rather than in a response. It is read from
 * `window.location` rather than through `useSearchParams` so this page does not
 * need a Suspense boundary, and the parameters are stripped afterwards so a
 * refresh does not re-announce it.
 *
 * Once per screen, not once per card: only one kind can be mid-consent, and a
 * hook on every card would have each of them checking the same query string.
 */
function useOauthOutcome() {
  const queryClient = useQueryClient();
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const outcome = params.get("google_ads");
    if (!outcome) return;

    if (outcome === "connected") {
      toast.success("Google Ads connected", { description: params.get("account") ?? undefined });
      void queryClient.invalidateQueries({ queryKey: keys.credentials });
    } else {
      toast.error("Google Ads was not connected", {
        description: params.get("reason") ?? undefined,
      });
    }
    params.delete("google_ads");
    params.delete("account");
    params.delete("reason");
    const query = params.toString();
    window.history.replaceState({}, "", `${window.location.pathname}${query ? `?${query}` : ""}`);
  }, [queryClient]);
}
