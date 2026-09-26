"use client";

import { useVirtualizer } from "@tanstack/react-virtual";
import { CircleCheck, CircleDashed, CircleX } from "lucide-react";
import { useRef, type ReactNode } from "react";

import { MonoId } from "@/components/creative/mono-id";
import { SegmentedControl } from "@/components/ui/segmented";
import type {
  ConformanceCheck,
  ConformanceConstraint,
  ConformanceFilter,
  ConformanceResponse,
  ConformanceSource,
} from "@/lib/api/creative-packages";
import { formatBytes } from "@/lib/creative/package";
import { cn } from "@/lib/utils";

const CONSTRAINT: Record<ConformanceConstraint, { label: string; op: string }> = {
  max_chars: { label: "Characters", op: "≤" },
  min_px: { label: "Minimum size", op: "≥" },
  ratio: { label: "Aspect ratio", op: "=" },
  max_bytes: { label: "File size", op: "≤" },
  format: { label: "Format", op: "∈" },
  min_duration_s: { label: "Minimum duration", op: "≥" },
  max_duration_s: { label: "Maximum duration", op: "≤" },
  fps: { label: "Frame rate", op: "=" },
  codec: { label: "Video codec", op: "=" },
  audio_codec: { label: "Audio codec", op: "=" },
};

const SOURCE: Record<ConformanceSource, string> = { lint: "Linter", pillow: "Pillow decode", ffprobe: "ffprobe" };

const UNCHECKED: Record<ConformanceResponse["unchecked"][number]["reason"], string> = {
  spec_missing: "No spec at the pin",
  file_missing: "File missing",
  file_unreadable: "File unreadable",
};

/** Row height estimate for the first paint; `measureElement` does the real work. */
const ROW_ESTIMATE = 44;

function value(constraint: ConformanceConstraint, raw: string | number): string {
  if (typeof raw === "number") {
    if (constraint === "max_bytes") return formatBytes(raw);
    if (constraint === "min_duration_s" || constraint === "max_duration_s") return `${raw} s`;
    if (constraint === "fps") return `${raw} fps`;
    if (constraint === "min_px") return `${raw} px`;
  }
  return String(raw);
}

/**
 * `SpecConformanceTable` — 4.6.1's measurements: every constraint of every
 * asset, what the pin's spec expects, what was measured and by what (Stage 04
 * PRD §15.4 K). Filtered by verdict **on the server** (`?verdict=`), and
 * virtualised, because a full slate measures thousands of constraints.
 *
 * Unchecked assets are listed under the table whatever the filter: an asset
 * nothing could be checked against is neither a pass nor a fail, and it must
 * not vanish behind one.
 */
export function SpecConformanceTable({
  data,
  filter,
  onFilter,
  assetName,
  loading,
}: {
  data: ConformanceResponse;
  filter: ConformanceFilter;
  onFilter: (filter: ConformanceFilter) => void;
  /** What an asset is, in words — from the run's asset list. */
  assetName: (assetId: string) => string | null;
  loading: boolean;
}) {
  const viewport = useRef<HTMLDivElement>(null);
  const checks = data.checks;
  const rows = useVirtualizer({
    count: checks.length,
    getScrollElement: () => viewport.current,
    estimateSize: () => ROW_ESTIMATE,
    measureElement: (element) => element.getBoundingClientRect().height,
    overscan: 12,
  });

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <SegmentedControl<ConformanceFilter>
          label="Filter by verdict"
          value={filter}
          onChange={onFilter}
          options={[
            { value: "all", label: "Every check" },
            { value: "fail", label: `Fails · ${data.failed}` },
            { value: "pass", label: "Passes" },
          ]}
        />
        <p className="text-sm text-fg-muted tabular-nums" aria-live="polite" data-testid="conformance-count">
          {loading ? "Filtering…" : `${checks.length} ${checks.length === 1 ? "check" : "checks"}`} · ruleset{" "}
          <span className="font-mono text-xs">{data.ruleset_version}</span>
        </p>
      </div>

      <div className="overflow-hidden rounded-token border">
        <div role="table" aria-label="Spec conformance" aria-rowcount={checks.length + 1} className="flex flex-col">
          <div
            role="row"
            aria-rowindex={1}
            className="hidden border-b bg-surface px-4 py-2 text-xs font-medium text-fg-subtle sm:grid sm:grid-cols-12 sm:gap-3"
          >
            <span role="columnheader" className="col-span-4">Asset</span>
            <span role="columnheader" className="col-span-2">Constraint</span>
            <span role="columnheader" className="col-span-2">Expected</span>
            <span role="columnheader" className="col-span-2">Measured</span>
            <span role="columnheader" className="col-span-1">Source</span>
            <span role="columnheader" className="col-span-1">Verdict</span>
          </div>
          <div ref={viewport} className="max-h-96 overflow-y-auto" data-testid="conformance-viewport">
            {checks.length === 0 ? (
              <p className="px-4 py-8 text-center text-sm text-fg-muted">
                {filter === "fail"
                  ? "Nothing fails. Every measured constraint meets the pin's spec."
                  : "No measured constraint matches this filter."}
              </p>
            ) : (
              <div style={{ height: rows.getTotalSize(), position: "relative" }}>
                {rows.getVirtualItems().map((item) => {
                  const check = checks[item.index];
                  if (!check) return null;
                  return (
                    <Row
                      key={`${check.asset_id}-${check.media_id ?? ""}-${check.constraint}-${item.index}`}
                      check={check}
                      index={item.index}
                      name={assetName(check.asset_id)}
                      measure={rows.measureElement}
                      start={item.start}
                    />
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </div>

      {data.unchecked.length > 0 ? (
        <div className="flex flex-col gap-2" data-testid="conformance-unchecked">
          <h3 className="text-sm font-medium text-fg">
            Unchecked <span className="font-normal text-fg-muted tabular-nums">· {data.unchecked.length}</span>
          </h3>
          <ul className="flex flex-col divide-y rounded-token border text-sm">
            {data.unchecked.map((item) => (
              <li key={`${item.asset_id}-${item.media_id ?? ""}-${item.reason}`} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-4 py-2.5">
                <CircleDashed className="size-3.5 shrink-0 translate-y-0.5 text-fg-subtle" aria-hidden />
                <span className="font-medium text-fg">{UNCHECKED[item.reason]}</span>
                <span className="min-w-0 flex-1 text-fg-muted">
                  {assetName(item.asset_id) ?? "Asset"} <MonoId value={item.asset_id} label="asset id" /> — {item.detail}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function Row({
  check,
  index,
  name,
  measure,
  start,
}: {
  check: ConformanceCheck;
  index: number;
  name: string | null;
  measure: (element: Element | null) => void;
  start: number;
}) {
  const { label, op } = CONSTRAINT[check.constraint];
  const failed = check.verdict === "fail";
  return (
    <div
      role="row"
      aria-rowindex={index + 2}
      ref={measure}
      data-index={index}
      data-verdict={check.verdict}
      data-testid="conformance-row"
      className="absolute inset-x-0 grid grid-cols-2 gap-x-3 gap-y-1 border-b px-4 py-2.5 text-sm sm:grid-cols-12 sm:items-center"
      style={{ transform: `translateY(${start}px)` }}
    >
      <Cell className="col-span-2 sm:col-span-4">
        <span className="block truncate text-fg" title={name ?? undefined}>
          {name ?? "Asset"}
        </span>
        <span className="flex flex-wrap items-center gap-x-2 text-xs text-fg-subtle">
          <MonoId value={check.asset_id} label="asset id" />
          {check.media_id ? (
            <>
              file <MonoId value={check.media_id} label="media id" />
            </>
          ) : null}
        </span>
      </Cell>
      <Cell className="sm:col-span-2" mobileLabel="Constraint">
        {label}
      </Cell>
      <Cell className="sm:col-span-2 tabular-nums" mobileLabel="Expected">
        {op} {value(check.constraint, check.expected)}
      </Cell>
      <Cell className={cn("sm:col-span-2 tabular-nums", failed && "font-medium")} mobileLabel="Measured">
        {value(check.constraint, check.measured)}
      </Cell>
      <Cell className="text-fg-muted sm:col-span-1" mobileLabel="Source">
        {SOURCE[check.source]}
      </Cell>
      <Cell className="sm:col-span-1" mobileLabel="Verdict">
        <span className="inline-flex items-center gap-1">
          {failed ? (
            <CircleX className="size-3.5 text-status-failed-ink" aria-hidden />
          ) : (
            <CircleCheck className="size-3.5 text-status-success" aria-hidden />
          )}
          {failed ? "Fail" : "Pass"}
        </span>
      </Cell>
    </div>
  );
}

function Cell({ className, mobileLabel, children }: { className?: string; mobileLabel?: string; children: ReactNode }) {
  return (
    <div role="cell" className={cn("min-w-0", className)}>
      {mobileLabel ? <span className="block text-xs text-fg-subtle sm:hidden">{mobileLabel}</span> : null}
      {children}
    </div>
  );
}
