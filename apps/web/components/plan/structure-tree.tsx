"use client";

/**
 * The account structure: campaign -> ad group -> keyword (Stage 02 PRD §15.3 D).
 *
 * §15.4 rule 1 sets the bar: 40 campaigns, 400 ad groups and 4,000 keywords
 * inside a 16 ms frame budget, virtualised. Two decisions get it there.
 *
 * **The tree is flattened to the rows that are actually visible, and one
 * virtualiser covers all three levels.** A virtualiser per level — or a
 * recursive render with a windowed list per ad group — measures and re-measures
 * hundreds of nested scrollers, which is how a "virtualised" tree ends up
 * slower than the naive one. Here, expanding a campaign splices its ad groups
 * into a single flat array and the window is recomputed once.
 *
 * **Expansion state is a Set of row keys, and the flat array is memoised on
 * it.** Rebuilding 4,440 rows on every scroll frame would eat the budget on its
 * own; rebuilt on expansion only, it costs nothing anybody can see.
 *
 * Collapsed by default. Forty campaigns is what the reader came for; four
 * thousand keywords is what they drill into.
 *
 * What is NOT here: search and filtering. The PRD does not ask for them and the
 * plan is the artifact of record — a reader looking for one keyword is looking
 * at the wrong screen, and the Editor CSV export is the one that answers it.
 */

import { useVirtualizer } from "@tanstack/react-virtual";
import { Check, ChevronDown, ChevronRight, ListTree, TriangleAlert } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip } from "@/components/ui/tooltip";
import type {
  PlanStructureAdGroup,
  PlanStructureCampaign,
  PlanStructureKeyword,
  PlanStructurePage,
} from "@/lib/api/plan";
import { compactNumber, usd } from "@/lib/format";
import { cn } from "@/lib/utils";

type Row =
  | { kind: "campaign"; key: string; campaign: PlanStructureCampaign }
  | { kind: "ad_group"; key: string; parentKey: string; group: PlanStructureAdGroup }
  | { kind: "keyword"; key: string; keyword: PlanStructureKeyword }
  | { kind: "more"; key: string };

/** Estimates only — every row measures itself once it is mounted. */
const HEIGHT: Record<Row["kind"], number> = {
  campaign: 76,
  ad_group: 72,
  keyword: 34,
  more: 56,
};

const INDENT: Record<Row["kind"], string> = {
  campaign: "pl-3",
  ad_group: "pl-9",
  keyword: "pl-16",
  more: "pl-3",
};

export function StructureTree({
  pages,
  loading,
  hasNextPage,
  fetchingNextPage,
  onNeedMore,
}: {
  pages: PlanStructurePage[];
  loading: boolean;
  hasNextPage: boolean;
  fetchingNextPage: boolean;
  onNeedMore: () => void;
}) {
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());

  const campaigns = useMemo(() => pages.flatMap((page) => page.campaigns), [pages]);
  const meta = pages[0];

  const rows = useMemo<Row[]>(() => {
    const flat: Row[] = [];
    for (const campaign of campaigns) {
      const key = `${campaign.campaign_ref}@${campaign.market ?? ""}`;
      flat.push({ kind: "campaign", key, campaign });
      if (!expanded.has(key)) continue;
      for (const group of campaign.ad_groups) {
        const groupKey = `${key}|${group.name}`;
        flat.push({ kind: "ad_group", key: groupKey, parentKey: key, group });
        if (!expanded.has(groupKey)) continue;
        for (const keyword of group.keywords) {
          flat.push({ kind: "keyword", key: `${groupKey}|${keyword.term}`, keyword });
        }
      }
    }
    if (hasNextPage) flat.push({ kind: "more", key: "__more" });
    return flat;
  }, [campaigns, expanded, hasNextPage]);

  const scroller = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scroller.current,
    estimateSize: (index) => HEIGHT[rows[index]?.kind ?? "keyword"],
    overscan: 12,
    getItemKey: (index) => rows[index]?.key ?? index,
  });

  const items = virtualizer.getVirtualItems();

  // Fetch the next page when its placeholder row is actually reached, rather
  // than on a scroll-position threshold: the row is in the list, so "is it
  // visible" is a question the virtualiser has already answered.
  const lastVisible = items.at(-1);
  useEffect(() => {
    if (!lastVisible) return;
    if (rows[lastVisible.index]?.kind === "more" && !fetchingNextPage) onNeedMore();
  }, [lastVisible, rows, fetchingNextPage, onNeedMore]);

  const toggle = useCallback((key: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (!next.delete(key)) next.add(key);
      return next;
    });
  }, []);

  const expandAll = useCallback(() => {
    const next = new Set<string>();
    for (const campaign of campaigns) {
      const key = `${campaign.campaign_ref}@${campaign.market ?? ""}`;
      next.add(key);
      for (const group of campaign.ad_groups) next.add(`${key}|${group.name}`);
    }
    setExpanded(next);
  }, [campaigns]);

  if (loading) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-[76px] w-full" />
        <Skeleton className="h-[76px] w-full" />
        <Skeleton className="h-[76px] w-full" />
      </div>
    );
  }

  if (campaigns.length === 0) {
    return (
      <EmptyState
        icon={ListTree}
        title="No account structure yet"
        description="The structure is built by node 2.4.2, once the budget gate is approved. Until then there is no tree to show."
      />
    );
  }

  const totals = meta?.totals;

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <p className="text-xs text-fg-muted">
          {totals ? (
            // Exact, not compact. `compactNumber(4000)` is "4K", and a plan
            // with 4,000 keywords is a plan somebody is about to sign off —
            // this is the audit surface, and the freeze dialog quotes the same
            // three numbers precisely. Compact notation belongs on an axis tick
            // and on a seven-figure pipeline, not here.
            <>
              <span data-numeric>{totals.campaigns}</span> campaigns ·{" "}
              <span data-numeric>{totals.ad_groups}</span> ad groups ·{" "}
              <span data-numeric>{totals.keywords}</span> keywords
            </>
          ) : null}
          {campaigns.length < (totals?.campaigns ?? 0) ? (
            <span className="text-fg-subtle">
              {" "}
              · showing the first <span data-numeric>{campaigns.length}</span>
            </span>
          ) : null}
        </p>
        <div className="flex gap-3 text-xs">
          <button
            type="button"
            onClick={expandAll}
            className="text-accent hover:underline"
            disabled={campaigns.length === 0}
          >
            Expand all
          </button>
          <button
            type="button"
            onClick={() => setExpanded(new Set())}
            className="text-fg-muted hover:text-fg"
          >
            Collapse all
          </button>
        </div>
      </div>

      {meta?.invalid_names.length ? (
        <p className="text-xs text-status-gate">
          <span data-numeric>{meta.invalid_names.length}</span>{" "}
          {meta.invalid_names.length === 1 ? "name does" : "names do"} not match the convention:{" "}
          <span className="font-mono">{meta.invalid_names.slice(0, 3).join(", ")}</span>
          {meta.invalid_names.length > 3 ? " and others" : ""}.
        </p>
      ) : null}

      {/* Its own scroller, bounded. A window over the rows needs a viewport to
          be a window of, and a 4,440-row tree inside the page's own scroll
          would defeat the virtualiser the moment the reader expanded it. */}
      <div
        ref={scroller}
        className="max-h-[70vh] min-h-64 overflow-y-auto rounded-[var(--radius)] border bg-surface-raised"
      >
        {/* Deliberately not `role="tree"`. A tree role promises arrow-key
            navigation, a roving tab stop and a selection model, and this widget
            has none of those — rows are expanded, never selected. Claiming the
            role would announce a keyboard contract to a screen-reader user that
            nothing here honours, which is worse than the plain disclosures it
            actually is. So: a labelled region of nested disclosure buttons,
            each carrying its own `aria-expanded` on the control rather than on
            a wrapper, and each naming its level in its accessible name. */}
        <div aria-label="Account structure" style={{ height: virtualizer.getTotalSize() }}>
          <div
            style={{
              transform: `translateY(${items[0]?.start ?? 0}px)`,
            }}
          >
            {items.map((item) => {
              const row = rows[item.index];
              // The window can name an index the rows array no longer holds for
              // one frame after a collapse. Rendering nothing beats throwing.
              if (!row) return null;
              return (
                <div
                  key={item.key}
                  data-index={item.index}
                  ref={virtualizer.measureElement}
                  className={cn(
                    "border-b last:border-0",
                    INDENT[row.kind],
                    row.kind === "keyword" && "bg-surface",
                  )}
                >
                  {row.kind === "campaign" ? (
                    <CampaignRow
                      campaign={row.campaign}
                      open={expanded.has(row.key)}
                      onToggle={() => toggle(row.key)}
                    />
                  ) : row.kind === "ad_group" ? (
                    <AdGroupRow
                      group={row.group}
                      open={expanded.has(row.key)}
                      onToggle={() => toggle(row.key)}
                    />
                  ) : row.kind === "keyword" ? (
                    <KeywordRow keyword={row.keyword} />
                  ) : (
                    <p className="py-4 pr-3 text-xs text-fg-subtle">
                      {fetchingNextPage ? "Loading more campaigns…" : "Scroll for more campaigns"}
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}

function Disclosure({ open }: { open: boolean }) {
  // Two icons, never one rotated: a 90deg transform on a 14px chevron renders
  // as an axis-aligned corner rather than a caret.
  return open ? (
    <ChevronDown aria-hidden className="mt-0.5 size-3.5 shrink-0 text-fg-subtle" />
  ) : (
    <ChevronRight aria-hidden className="mt-0.5 size-3.5 shrink-0 text-fg-subtle" />
  );
}

function CampaignRow({
  campaign,
  open,
  onToggle,
}: {
  campaign: PlanStructureCampaign;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={open}
      className="flex w-full items-start gap-2 py-3 pr-3 text-left transition-colors hover:bg-surface-hover"
    >
      <Disclosure open={open} />
      <span className="min-w-0 flex-1">
        <span className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span className="sr-only">Campaign: </span>
          <span className="font-mono text-xs break-all text-fg">{campaign.name}</span>
          <NameTick valid={campaign.name_valid} />
        </span>
        <span className="mt-1 flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-xs text-fg-muted">
          {campaign.market ? <span>{campaign.market}</span> : null}
          {campaign.type ? <span className="text-fg-subtle">{label(campaign.type)}</span> : null}
          {campaign.monthly_budget_usd !== null ? (
            <span data-numeric>{usd(campaign.monthly_budget_usd, 0)}/mo</span>
          ) : null}
          {campaign.bid_strategy ? (
            <span className="text-fg-subtle">{label(campaign.bid_strategy)}</span>
          ) : null}
          <span data-numeric className="text-fg-subtle">
            {campaign.ad_group_count} ad groups · {campaign.keyword_count} keywords
          </span>
        </span>
      </span>
      <ThresholdBadge campaign={campaign} />
    </button>
  );
}

function AdGroupRow({
  group,
  open,
  onToggle,
}: {
  group: PlanStructureAdGroup;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={open}
      className="flex w-full items-start gap-2 py-2.5 pr-3 text-left transition-colors hover:bg-surface-hover"
    >
      <Disclosure open={open} />
      <span className="min-w-0 flex-1">
        <span className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span className="sr-only">Ad group: </span>
          <span className="font-mono text-xs break-all text-fg">{group.name}</span>
          <NameTick valid={group.name_valid} />
          <span data-numeric className="text-xs text-fg-subtle">
            {group.keyword_count} keywords
          </span>
        </span>
        {group.primary_message ? (
          <span className="mt-1 block text-xs text-fg-muted">{group.primary_message}</span>
        ) : null}
        {group.landing_url ? (
          // Not a link: this is the plan's intended destination, not a page to
          // visit mid-review, and a nested anchor inside the disclosure button
          // is invalid markup that swallows the toggle.
          <span className="mt-0.5 block truncate font-mono text-[0.6875rem] text-fg-subtle">
            {group.landing_url}
          </span>
        ) : (
          <span className="mt-0.5 block text-xs text-status-gate">No landing URL</span>
        )}
      </span>
    </button>
  );
}

function KeywordRow({ keyword }: { keyword: PlanStructureKeyword }) {
  return (
    <div className="flex items-baseline gap-2 py-1.5 pr-3 text-xs">
      <span className="min-w-0 flex-1 truncate text-fg-muted">
        <span className="sr-only">Keyword: </span>
        {keyword.term}
      </span>
      {keyword.match_type ? (
        <span className="shrink-0 text-fg-subtle">{keyword.match_type}</span>
      ) : null}
      {keyword.search_volume !== null ? (
        <span data-numeric className="w-14 shrink-0 text-right text-fg-subtle">
          {compactNumber(keyword.search_volume)}
        </span>
      ) : null}
      {keyword.forecast_cpc_usd !== null ? (
        <span data-numeric className="w-12 shrink-0 text-right text-fg-muted">
          {usd(keyword.forecast_cpc_usd)}
        </span>
      ) : null}
    </div>
  );
}

/**
 * Whether this name passes 2.4.1's convention.
 *
 * Three states, and the third is the point: `null` is *unchecked*, which
 * happens when 2.4.1 emitted no `validator_regex`. A green tick on an
 * unchecked name would be the screen asserting something nobody verified, so
 * an unchecked name shows nothing at all.
 */
function NameTick({ valid }: { valid: boolean | null }) {
  if (valid === null) return null;
  if (valid) {
    return (
      <Tooltip content="Matches the naming convention">
        <span className="inline-flex">
          <Check aria-hidden className="size-3 text-status-success" />
          <span className="sr-only">Name matches the convention</span>
        </span>
      </Tooltip>
    );
  }
  return (
    <Tooltip content="Does not match the naming convention 2.4.1 set">
      <span className="inline-flex items-center gap-1 text-status-gate">
        <TriangleAlert aria-hidden className="size-3" />
        <span className="sr-only">Name does not match the convention</span>
      </span>
    </Tooltip>
  );
}

/**
 * 2.4.3's per-campaign verdict.
 *
 * The badge names the threshold and the shortfall rather than saying "below" —
 * a campaign forecast at 12 conversions against a threshold of 30 is a decision
 * about merging two campaigns, and the two numbers are what makes it one.
 */
function ThresholdBadge({ campaign }: { campaign: PlanStructureCampaign }) {
  if (!campaign.verdict) return null;
  const clears = campaign.verdict === "clears";
  const detail =
    campaign.forecast_conv_30d !== null && campaign.threshold !== null
      ? `${campaign.forecast_conv_30d} forecast conversions in 30 days against a threshold of ${campaign.threshold}.`
      : campaign.reason;

  return (
    <Tooltip
      content={
        <span className="block max-w-64">
          {detail}
          {campaign.remedy ? <span className="mt-1 block">{campaign.remedy}</span> : null}
        </span>
      }
    >
      <span
        className={cn(
          "mt-0.5 shrink-0 rounded-full border px-2 py-0.5 text-[0.6875rem] font-medium",
          clears
            ? "border-border text-fg-muted"
            : "border-[var(--status-gate)] text-status-gate",
        )}
      >
        {clears ? "Clears" : "Below threshold"}
      </span>
    </Tooltip>
  );
}

function label(value: string): string {
  return value.replace(/_/g, " ");
}
