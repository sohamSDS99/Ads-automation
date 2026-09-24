"use client";

import { useVirtualizer } from "@tanstack/react-virtual";
import { ChevronDown, ExternalLink, FileSearch, Images, Rows3, Search, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { ScreenshotGallery } from "@/components/evidence/gallery";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { JsonTree } from "@/components/ui/json-tree";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import {
  SOURCE_LABEL,
  evidenceSummary,
  screenshotUrl,
  type EvidenceItem,
  type EvidenceQuery,
  type EvidenceSource,
} from "@/lib/api/evidence";
import { absoluteTime, relativeTime } from "@/lib/format";
import { errorMessage, useEvidence } from "@/lib/queries";
import { cn } from "@/lib/utils";

/**
 * The evidence explorer (PRD §13.4 D).
 *
 * Virtualized because this is the one screen in the product with volume — a
 * single run writes thousands of rows, and the browse is the check on every
 * claim any report makes (PRD Law 1).
 *
 * Search is hybrid on the server: full text and vector, fused. The `matched_by`
 * chip says which retriever found a row, because "why is this here" is part of
 * the answer when the match is semantic rather than literal.
 */
const ROW_ESTIMATE = 74;
const PAGE = 50;

export function EvidenceExplorer({
  projectId,
  initial,
  className,
}: {
  /** Scopes the whole screen to one project; omitted on the workspace view. */
  projectId?: string;
  initial?: { ids?: string[]; runId?: string; source?: EvidenceSource };
  className?: string;
}) {
  const [text, setText] = useState("");
  const search = useDebounced(text, 300);
  const [source, setSource] = useState<EvidenceSource | "">(initial?.source ?? "");
  const [kind, setKind] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [ids, setIds] = useState<string[] | undefined>(initial?.ids);
  const [view, setView] = useState<"table" | "gallery">("table");

  const query: EvidenceQuery = {
    project_id: projectId,
    run_id: initial?.runId,
    ids,
    source: source || undefined,
    kind: kind.trim() || undefined,
    q: search.trim() || undefined,
    fetched_from: from ? new Date(from).toISOString() : undefined,
    fetched_to: to ? endOfDay(to) : undefined,
    limit: PAGE,
  };

  const page = useEvidence(query);
  const items = useMemo(
    () => page.data?.pages.flatMap((one) => one.items) ?? [],
    [page.data],
  );
  const ranked = page.data?.pages[0]?.ranked ?? false;
  const kinds = useMemo(() => [...new Set(items.map((item) => item.kind))].sort(), [items]);
  const withShots = items.filter((item) => item.has_screenshot);

  return (
    <div className={cn("flex min-h-0 flex-col gap-4", className)}>
      <div className="flex flex-wrap items-end gap-2">
        <label className="min-w-56 flex-1">
          <span className="sr-only">Search evidence</span>
          <span className="relative block">
            <Search
              aria-hidden
              className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-fg-subtle"
            />
            <Input
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder="Search — exact terms and meaning both"
              className="pl-9"
              type="search"
            />
          </span>
        </label>

        <label className="w-44">
          <span className="sr-only">Source</span>
          <Select value={source} onChange={(event) => setSource(event.target.value as EvidenceSource | "")}>
            <option value="">Every source</option>
            {(Object.keys(SOURCE_LABEL) as EvidenceSource[]).map((one) => (
              <option key={one} value={one}>
                {SOURCE_LABEL[one]}
              </option>
            ))}
          </Select>
        </label>

        <label className="w-44">
          <span className="sr-only">Kind</span>
          <Input
            value={kind}
            onChange={(event) => setKind(event.target.value)}
            placeholder="Kind"
            list="evidence-kinds"
          />
          <datalist id="evidence-kinds">
            {kinds.map((one) => (
              <option key={one} value={one} />
            ))}
          </datalist>
        </label>

        <label className="text-xs text-fg-muted">
          <span className="mb-1 block">Fetched from</span>
          <Input type="date" value={from} onChange={(event) => setFrom(event.target.value)} />
        </label>
        <label className="text-xs text-fg-muted">
          <span className="mb-1 block">to</span>
          <Input type="date" value={to} onChange={(event) => setTo(event.target.value)} />
        </label>

        {withShots.length > 0 ? (
          <div className="flex rounded-[var(--radius)] border p-0.5">
            <ViewToggle
              active={view === "table"}
              onClick={() => setView("table")}
              icon={Rows3}
              label="Rows"
            />
            <ViewToggle
              active={view === "gallery"}
              onClick={() => setView("gallery")}
              icon={Images}
              label="Creatives"
            />
          </div>
        ) : null}
      </div>

      {ids ? (
        <div className="flex items-center justify-between gap-3 rounded-[var(--radius)] border border-accent/40 bg-accent-soft px-3 py-2 text-sm">
          <span className="text-fg">
            Showing {ids.length} cited {ids.length === 1 ? "row" : "rows"} from a report.
          </span>
          <Button variant="ghost" size="sm" onClick={() => setIds(undefined)}>
            <X aria-hidden />
            Show everything
          </Button>
        </div>
      ) : null}

      {page.isError ? (
        <Alert tone="error" title="Evidence could not be loaded">
          {errorMessage(page)}
        </Alert>
      ) : null}

      {page.isPending ? <Skeleton className="h-64 w-full" /> : null}

      {page.data && items.length === 0 ? (
        <EmptyState
          icon={FileSearch}
          title={search || kind || source ? "Nothing matches that" : "No evidence yet"}
          description={
            search || kind || source
              ? "Try fewer filters, or search for a phrase rather than an exact token."
              : "Connectors write rows here as runs gather them, and a CSV upload writes them directly."
          }
        />
      ) : null}

      {items.length > 0 && view === "gallery" ? (
        <ScreenshotGallery items={withShots} />
      ) : null}

      {items.length > 0 && view === "table" ? (
        <>
          <p className="text-xs text-fg-subtle">
            {items.length} shown{page.hasNextPage ? ", more below" : ""}
            {ranked ? " · ordered by relevance" : ""}
          </p>
          <VirtualRows
            items={items}
            projectId={projectId}
            onReachEnd={() => {
              if (page.hasNextPage && !page.isFetchingNextPage) void page.fetchNextPage();
            }}
          />
          {page.isFetchingNextPage ? (
            <p className="flex items-center gap-2 text-xs text-fg-subtle">
              <Spinner /> Loading more
            </p>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function VirtualRows({
  items,
  projectId,
  onReachEnd,
}: {
  items: EvidenceItem[];
  projectId?: string;
  onReachEnd: () => void;
}) {
  const viewport = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const rows = useVirtualizer({
    count: items.length,
    getScrollElement: () => viewport.current,
    estimateSize: () => ROW_ESTIMATE,
    overscan: 8,
  });

  const virtual = rows.getVirtualItems();
  const last = virtual.at(-1);
  useEffect(() => {
    if (last && last.index >= items.length - 8) onReachEnd();
  }, [last, items.length, onReachEnd]);

  return (
    <div
      ref={viewport}
      // Height follows the rows up to a ceiling, rather than always filling it:
      // a three-row result in a 40rem frame reads as a loading state.
      className="overflow-y-auto rounded-[var(--radius)] border bg-surface-raised"
      style={{ maxHeight: "min(60vh, 40rem)" }}
    >
      <div style={{ height: rows.getTotalSize(), position: "relative" }}>
        {virtual.map((row) => {
          const item = items[row.index]!;
          return (
            <div
              key={item.id}
              data-index={row.index}
              // Measured rather than estimated: an expanded row is as tall as
              // its payload, and a virtualizer guessing that would jitter.
              ref={rows.measureElement}
              className="absolute inset-x-0 top-0"
              style={{ transform: `translateY(${row.start}px)` }}
            >
              <Row
                item={item}
                projectId={projectId}
                open={expanded.has(item.id)}
                onToggle={() =>
                  setExpanded((current) => {
                    const next = new Set(current);
                    if (!next.delete(item.id)) next.add(item.id);
                    return next;
                  })
                }
              />
            </div>
          );
        })}
      </div>
    </div>
  );
}

function Row({
  item,
  projectId,
  open,
  onToggle,
}: {
  item: EvidenceItem;
  projectId?: string;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <article className="border-b last:border-b-0">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="flex w-full items-start gap-3 px-4 py-3 text-left transition-colors hover:bg-surface-hover"
      >
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className="text-sm font-medium text-fg">{SOURCE_LABEL[item.source]}</span>
            <span className="font-mono text-[0.6875rem] text-fg-subtle">{item.kind}</span>
            {item.matched_by !== "filter" ? (
              <span className="rounded-full border px-1.5 text-[0.6875rem] text-fg-subtle">
                {item.matched_by === "both" ? "text + meaning" : item.matched_by}
              </span>
            ) : null}
          </span>
          <span className="mt-1 block line-clamp-2 text-sm text-fg-muted">
            {evidenceSummary(item)}
          </span>
        </span>
        <time
          className="shrink-0 text-xs text-fg-subtle"
          dateTime={item.fetched_at}
          title={absoluteTime(item.fetched_at)}
        >
          {relativeTime(item.fetched_at)}
        </time>
        <ChevronDown
          aria-hidden
          className={cn("size-4 shrink-0 text-fg-subtle transition-transform", open && "rotate-180")}
        />
      </button>

      {open ? (
        <div className="space-y-3 border-t bg-surface px-4 py-3">
          <div className="flex flex-wrap items-center gap-4">
            {item.source_url ? (
              <a
                href={item.source_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-sm text-accent hover:underline"
              >
                Open the source
                <ExternalLink className="size-3.5" aria-hidden />
              </a>
            ) : null}
            <span className="font-mono text-xs text-fg-subtle">{item.id}</span>
            {item.run_id ? (
              <span className="text-xs text-fg-subtle">
                gathered by run <span className="font-mono">{item.run_id.slice(0, 8)}</span>
              </span>
            ) : (
              <span className="text-xs text-fg-subtle">uploaded outside a run</span>
            )}
          </div>

          {item.has_screenshot ? (
            <a
              href={screenshotUrl(item.id)}
              target="_blank"
              rel="noreferrer"
              className="block w-fit overflow-hidden rounded-[var(--radius)] border"
            >
              {/* The capture is a full-page grab of a competitor's ad grid;
                  the thumbnail is a crop of its top, which is where the ad is.
                  Not `next/image`: the optimizer would fetch it from the Next
                  server, which carries no session cookie, so every capture
                  would come back a 401. */}
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={screenshotUrl(item.id)}
                alt={`Stored capture for ${item.kind}`}
                loading="lazy"
                className="h-40 w-auto max-w-full object-cover object-top"
              />
            </a>
          ) : null}

          <div className="max-h-80 overflow-y-auto rounded-[var(--radius)] border bg-surface-raised p-2.5">
            <JsonTree value={item.payload} />
          </div>

          {projectId ? null : (
            <p className="text-xs text-fg-subtle">
              Project <span className="font-mono">{item.project_id.slice(0, 8)}</span>
            </p>
          )}
        </div>
      ) : null}
    </article>
  );
}

function ViewToggle({
  active,
  onClick,
  icon: Icon,
  label,
}: {
  active: boolean;
  onClick: () => void;
  icon: typeof Rows3;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-[calc(var(--radius)-4px)] px-2.5 py-1.5 text-xs transition-colors",
        active ? "bg-accent-soft text-accent-soft-fg" : "text-fg-muted hover:text-fg",
      )}
    >
      <Icon className="size-3.5" aria-hidden />
      {label}
    </button>
  );
}

/** Typing is not a query. One request per pause, not one per keystroke. */
function useDebounced<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return settled;
}

/** A date input means the whole day, and the API compares against an instant. */
function endOfDay(date: string): string {
  const end = new Date(date);
  end.setHours(23, 59, 59, 999);
  return end.toISOString();
}
