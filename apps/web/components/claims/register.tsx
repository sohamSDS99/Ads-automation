"use client";

import { useVirtualizer } from "@tanstack/react-virtual";
import { PenLine, ScrollText, Search, UserCog } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";

import { ClaimStatusChip, ExpiryChip, RiskChip, claimTypeLabel } from "@/components/claims/chips";
import { ReassignOwnerDialog } from "@/components/claims/reassign-owner-dialog";
import { SignatureDrawer } from "@/components/claims/signature-drawer";
import { Alert } from "@/components/ui/alert";
import { Button, buttonVariants } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { isSignable, type ClaimList, type ClaimSummary } from "@/lib/api/claims";
import { useSession } from "@/lib/session";
import { cn } from "@/lib/utils";

/**
 * Row height *estimates*, not fixed heights.
 *
 * The virtualizer positions rows by absolute offset, so a fixed height that the
 * content outgrows does not clip — it **overlaps**, and the rows below render
 * on top of each other. That is exactly what happened at 390px with a flat 56:
 * a horizontal-overflow check passes cleanly while the table is unreadable,
 * which is why `measureElement` below is doing the real work and these two
 * numbers only decide the first paint.
 */
const ROW_ESTIMATE = 56;
const CARD_ESTIMATE = 148;

/** Below this the table becomes a card list (UI-SPEC §10). */
const COMPACT_QUERY = "(max-width: 767px)";

function useCompact(): boolean {
  const [compact, setCompact] = useState(false);
  useEffect(() => {
    const query = window.matchMedia(COMPACT_QUERY);
    const sync = () => setCompact(query.matches);
    sync();
    query.addEventListener("change", sync);
    return () => query.removeEventListener("change", sync);
  }, []);
  return compact;
}

/**
 * The claims register (PRD §15.3 C).
 *
 * Virtualized because §15.4 rule 1 sets a 16 ms frame budget at 500 claims, and
 * because the register is the screen somebody scrolls while deciding whether to
 * put their name on it — a table that stutters while being read is a table that
 * gets skimmed.
 *
 * **The signature control is rendered for the named legal owner and for nobody
 * else.** Not disabled: absent (§15.4 rule 3). A greyed-out sign button teaches
 * every approver in the workspace to go looking for the state that enables it,
 * and the honest answer — "this is not yours to sign" — is a sentence, not a
 * tooltip on a dead control.
 */
export function ClaimsRegister({
  guidelineId,
  data,
  loading,
  error,
  onRefetch,
  ownerName,
}: {
  guidelineId: string;
  data: ClaimList | undefined;
  loading: boolean;
  error: string | null;
  onRefetch: () => void;
  /** Resolved by the page from the sign-off matrix, so the table can name them. */
  ownerName: string | null;
}) {
  const { user, has } = useSession();
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  /**
   * The set the signer opened, captured at that moment.
   *
   * Not derived live from `data`. Two reasons, and the second one cost a
   * receipt: a background refetch must not change what somebody is part-way
   * through signing, and — once the signature lands — every claim leaves
   * `signable`, so a drawer whose existence depended on `canSign` unmounted
   * itself and destroyed the receipt it had just rendered. §15.3 C.5 says a
   * receipt you can only see once is not a receipt; one you see zero times is
   * worse.
   */
  const [session, setSession] = useState<{ claims: ClaimSummary[]; setHash: string } | null>(
    null,
  );
  const [reassign, setReassign] = useState(false);
  const viewport = useRef<HTMLDivElement>(null);
  const compact = useCompact();

  // Memoised, not `data?.claims ?? []`. That expression mints a new array every
  // render, so the filter below would re-run over all 500 rows on every
  // keystroke anywhere in the tree — the exact cost §15.4 rule 1 budgets away.
  const claims = useMemo(() => data?.claims ?? [], [data]);
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return claims.filter(
      (claim) =>
        (!status || claim.status === status) &&
        (!needle || claim.claim_text.toLowerCase().includes(needle)),
    );
  }, [claims, query, status]);

  const rows = useVirtualizer({
    count: filtered.length,
    getScrollElement: () => viewport.current,
    estimateSize: () => (compact ? CARD_ESTIMATE : ROW_ESTIMATE),
    // Measured, so a claim that wraps to three lines takes three lines of space
    // instead of sitting under the row below it.
    measureElement: (element) => element.getBoundingClientRect().height,
    overscan: 10,
  });

  const signable = claims.filter(isSignable);
  const isLegalOwner = Boolean(data?.legal_owner_id && data.legal_owner_id === user.id);
  // Three conditions, and all three are required. Holding `claim_sign` is the
  // role layer; being the named owner is the identity layer; a hash means there
  // is a set to sign at all.
  const canSign = isLegalOwner && has("claim_sign") && Boolean(data?.set_hash) && signable.length > 0;

  if (loading) {
    return (
      <div className="space-y-2" aria-busy>
        <Skeleton className="h-9 w-full" />
        {Array.from({ length: 6 }).map((_, index) => (
          <Skeleton key={index} className="h-14 w-full" />
        ))}
      </div>
    );
  }

  if (error) {
    return (
      <Alert tone="error" title="The register could not be read">
        {error}
        <Button variant="secondary" size="sm" className="mt-3" onClick={onRefetch}>
          Try again
        </Button>
      </Alert>
    );
  }

  if (claims.length === 0) {
    return (
      <EmptyState
        icon={ScrollText}
        title="No claims registered yet"
        description="Node 3.2.1 registers every claim it finds in your copy and evidence. Run the guideline stage and they will appear here for signature."
      />
    );
  }

  return (
    <div className="flex min-h-0 flex-col gap-3">
      <ActionBar
        canSign={canSign}
        isLegalOwner={isLegalOwner}
        ownerName={ownerName}
        hasOwner={Boolean(data?.legal_owner_id)}
        pending={signable.length}
        onSign={() =>
          data?.set_hash ? setSession({ claims: signable, setHash: data.set_hash }) : undefined
        }
        onReassign={has("user_manage") ? () => setReassign(true) : undefined}
      />

      <div className="flex flex-wrap items-center gap-2">
        <label className="min-w-56 flex-1">
          <span className="sr-only">Search claims</span>
          <span className="relative block">
            <Search
              aria-hidden
              className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-fg-subtle"
            />
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search the register"
              className="pl-9"
            />
          </span>
        </label>
        <Select
          value={status}
          onChange={(event) => setStatus(event.target.value)}
          aria-label="Filter by status"
        >
          <option value="">Every status</option>
          <option value="pending_signoff">Awaiting signature</option>
          <option value="approved">Approved</option>
          <option value="rejected">Rejected</option>
          <option value="unsupported">Unsupported</option>
          <option value="expired">Expired</option>
        </Select>
        <span data-numeric className="text-sm text-fg-subtle">
          {filtered.length} of {claims.length}
        </span>
      </div>

      {/* A real table, so a screen reader gets "row 340 of 500" rather than the
          twenty rows that happen to be mounted. */}
      <div className="min-h-0 overflow-hidden rounded-[var(--radius)] border">
        <div
          role="table"
          aria-label="Claims register"
          aria-rowcount={filtered.length}
          className="flex h-full min-h-0 flex-col"
        >
          {/* The header belongs to the table layout. In card form each row
              carries its own labels, and a header over cards is a header over
              nothing. */}
          {compact ? null : (
            <div
              role="row"
              className="grid shrink-0 grid-cols-[minmax(0,2.5fr)_repeat(4,minmax(0,1fr))] gap-3 border-b bg-surface px-4 py-2 text-xs font-medium text-fg-subtle"
            >
              <span role="columnheader">Claim</span>
              <span role="columnheader">Type</span>
              <span role="columnheader">Status</span>
              <span role="columnheader">Markets</span>
              <span role="columnheader">Expiry</span>
            </div>
          )}

          <div ref={viewport} className="max-h-[32rem] min-h-0 overflow-y-auto">
            {filtered.length === 0 ? (
              <p className="px-4 py-8 text-center text-sm text-fg-muted">
                No claim matches that filter.
              </p>
            ) : (
              <div style={{ height: rows.getTotalSize(), position: "relative" }}>
                {rows.getVirtualItems().map((item) => {
                  const claim = filtered[item.index];
                  // The virtualizer measures against a count that can change a
                  // frame before the list does, so an index past the end is a
                  // normal transient rather than an impossible state.
                  if (!claim) return null;
                  return (
                    <div
                      key={claim.id}
                      role="row"
                      aria-rowindex={item.index + 1}
                      ref={rows.measureElement}
                      data-index={item.index}
                      className={cn(
                        "absolute inset-x-0 border-b text-sm",
                        compact
                          ? "flex flex-col gap-1.5 px-4 py-3"
                          : "grid min-h-14 grid-cols-[minmax(0,2.5fr)_repeat(4,minmax(0,1fr))] items-center gap-3 px-4 py-2",
                      )}
                      style={{ transform: `translateY(${item.start}px)` }}
                    >
                      <span role="cell" className="min-w-0">
                        <span className={cn("text-fg", compact || "line-clamp-2 block")}>
                          {claim.claim_text}
                        </span>
                        <span className="mt-0.5 block text-xs text-fg-subtle">
                          <EvidenceNote claim={claim} />
                        </span>
                      </span>

                      {compact ? (
                        // One wrapping strip of labelled facts. A five-column
                        // grid at 390px is five columns of truncation.
                        <span className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
                          <span role="cell">
                            <ClaimStatusChip status={claim.status} />
                          </span>
                          <span role="cell" className="text-xs text-fg-muted">
                            {claimTypeLabel(claim.claim_type)}
                          </span>
                          <RiskChip tier={claim.risk_tier} />
                          <span role="cell" className="text-xs text-fg-muted">
                            {claim.market_scope.join(", ") || "All markets"}
                          </span>
                          <span role="cell">
                            <ExpiryChip claim={claim} />
                          </span>
                        </span>
                      ) : (
                        <>
                          <span role="cell" className="min-w-0">
                            <span className="block truncate text-xs text-fg-muted">
                              {claimTypeLabel(claim.claim_type)}
                            </span>
                            <RiskChip tier={claim.risk_tier} />
                          </span>
                          <span role="cell">
                            <ClaimStatusChip status={claim.status} />
                          </span>
                          <span role="cell" className="truncate text-xs text-fg-muted">
                            {claim.market_scope.join(", ") || "All"}
                          </span>
                          <span role="cell">
                            <ExpiryChip claim={claim} />
                          </span>
                        </>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </div>

      {session ? (
        <SignatureDrawer
          open
          // Closing is the only thing that refetches. Refetching on success
          // would pull the register out from under the receipt.
          onClose={() => {
            setSession(null);
            onRefetch();
          }}
          guidelineId={guidelineId}
          claims={session.claims}
          setHash={session.setHash}
          onSigned={() => undefined}
          onReRead={() => {
            setSession(null);
            onRefetch();
          }}
        />
      ) : null}

      {reassign && data?.legal_owner_id ? (
        <ReassignOwnerDialog
          open={reassign}
          onOpenChange={setReassign}
          currentOwnerName={ownerName}
          onDone={onRefetch}
        />
      ) : null}
    </div>
  );
}

/**
 * The sticky bar, which is the whole of "who may act" made visible.
 *
 * Four states, and the one that matters is the last: a project with no matrix
 * cannot route a signature at all, and the server answers `409 No sign-off
 * matrix`. Rendering a sign button there would send somebody into an error.
 */
function ActionBar({
  canSign,
  isLegalOwner,
  ownerName,
  hasOwner,
  pending,
  onSign,
  onReassign,
}: {
  canSign: boolean;
  isLegalOwner: boolean;
  ownerName: string | null;
  hasOwner: boolean;
  pending: number;
  onSign: () => void;
  onReassign?: () => void;
}) {
  const shared = "flex flex-wrap items-center justify-between gap-3 rounded-[var(--radius)] border px-4 py-3";

  if (!hasOwner) {
    return (
      <div className={cn(shared, "border-status-gate")}>
        <p className="text-sm text-fg">
          This project names no legal owner, so there is nobody a signature could be routed to.
        </p>
        <Link
          href="/approvals"
          className={cn(buttonVariants({ variant: "secondary", size: "sm" }))}
        >
          Decide G6
        </Link>
      </div>
    );
  }

  if (canSign) {
    return (
      <div className={cn(shared, "border-accent bg-accent-soft")}>
        <p data-numeric className="text-sm font-medium text-fg">
          {pending} {pending === 1 ? "claim awaits" : "claims await"} your signature
        </p>
        <Button size="sm" onClick={onSign}>
          <PenLine aria-hidden />
          Review and sign
        </Button>
      </div>
    );
  }

  return (
    <div className={cn(shared, "bg-surface")}>
      <p className="text-sm text-fg-muted">
        {isLegalOwner
          ? "Nothing in this register is waiting on your signature."
          : `Awaiting signature from ${ownerName ?? "the named legal owner"}.`}
      </p>
      {onReassign ? (
        <Button variant="secondary" size="sm" onClick={onReassign}>
          <UserCog aria-hidden />
          Reassign legal owner
        </Button>
      ) : null}
    </div>
  );
}

/** Zero evidence on a claim that is meant to be substantiated is worth saying. */
function EvidenceNote({ claim }: { claim: ClaimSummary }) {
  if (claim.evidence_ids.length > 0) {
    return <>{claim.evidence_ids.length} sources</>;
  }
  return <span className="text-status-failed">No evidence</span>;
}
