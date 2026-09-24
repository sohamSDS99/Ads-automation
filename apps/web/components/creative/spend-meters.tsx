import { TriangleAlert } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import type { CreativeSpend, SpendMeter } from "@/lib/api/runs";
import { usd } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Amber past four fifths, as the Stage 01 meter: the cap refuses the next
 * media job (law 43), so the warning has to arrive while a person can still
 * do something about it. A display threshold, not a verdict — the reserve
 * script is what holds the cap.
 */
const CLOSE = 0.8;

/**
 * `SpendMeters` — the Creative Console's two meters (Stage 04 PRD §15.4 C).
 *
 * Total (`max_creative_cost_usd`, text and media) and media
 * (`max_media_cost_usd`), each drawn as spent and reserved — two segments —
 * against its own cap: the three numbers the reserve script compares before it
 * grants a media job, read from `RunDetail.creative_spend` and never
 * recomputed here.
 *
 * dataviz: a meter is a single ratio against a limit. Spent is `series-1`;
 * reserved is the same hue one step lighter (`series-1` at 60%, validated as an
 * ordinal pair against `surface-raised` — 2.31:1 light, 2.56:1 dark); the
 * track is neutral, as in S4-P3's `CostEstimate`; a 2px surface gap separates
 * the segments; the data-end is rounded, the baseline square. Severity is a
 * chip with an icon rather than an amber fill: `status-gate`'s lighter step
 * cannot clear 2:1 on white, and status is never colour alone. The line of
 * numbers beside each bar is its legend, its labels and its accessible view.
 */
export function SpendMeters({ spend }: { spend: CreativeSpend | null | undefined }) {
  if (!spend) {
    return (
      <p className="text-xs text-fg-muted">
        Spend meters unavailable: reserved spend could not be read. They return with the next
        refresh.
      </p>
    );
  }
  return (
    <div className="flex flex-wrap items-start gap-x-6 gap-y-2" aria-label="Spend against both caps" role="group">
      <Meter label="Total" meter={spend.total} />
      <Meter label="Media" meter={spend.media} />
    </div>
  );
}

function Meter({ label, meter }: { label: string; meter: SpendMeter }) {
  const spent = Number(meter.spent_usd);
  const reserved = Number(meter.reserved_usd);
  const cap = Number(meter.cap_usd);
  const used = spent + reserved;
  const ratio = cap > 0 ? used / cap : 0;
  const spentPct = cap > 0 ? Math.min(100, (spent / cap) * 100) : 0;
  const reservedPct = cap > 0 ? Math.min(100 - spentPct, (reserved / cap) * 100) : 0;
  const percent = Math.round(ratio * 100);
  const summary = `${usd(spent)} spent and ${usd(reserved)} reserved of ${usd(cap)}, ${percent}% of the cap`;

  return (
    <div className="flex min-w-48 flex-col gap-1.5">
      <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-fg-muted">
        <span className="font-medium text-fg">{label}</span>
        <span className="inline-flex items-center gap-1 tabular-nums">
          <span aria-hidden className="size-2 rounded-sm bg-series-1" />
          {usd(spent)} spent
        </span>
        <span className="inline-flex items-center gap-1 tabular-nums">
          <span aria-hidden className="size-2 rounded-sm bg-series-1/60" />
          {usd(reserved)} reserved
        </span>
        <span className="tabular-nums">of {usd(cap)}</span>
        {ratio >= 1 ? (
          <Badge tone="danger">
            <TriangleAlert className="size-3 text-status-failed" aria-hidden />
            At the cap
          </Badge>
        ) : ratio >= CLOSE ? (
          <Badge tone="warning">
            <TriangleAlert className="size-3 text-status-gate" aria-hidden />
            {percent}% of cap
          </Badge>
        ) : null}
      </p>
      <div
        role="meter"
        aria-label={`${label} spend`}
        aria-valuemin={0}
        aria-valuemax={cap}
        aria-valuenow={Math.min(used, cap)}
        aria-valuetext={summary}
        title={summary}
        className="flex h-2 w-full overflow-hidden rounded-r bg-surface-hover"
      >
        <div className="flex h-full gap-0.5" style={{ width: `${spentPct + reservedPct}%` }}>
          {spentPct > 0 ? (
            <div
              className={cn("h-full bg-series-1", reservedPct === 0 && "rounded-r")}
              style={{ flexGrow: spentPct, flexBasis: 0 }}
            />
          ) : null}
          {reservedPct > 0 ? (
            <div
              className="h-full rounded-r bg-series-1/60"
              style={{ flexGrow: reservedPct, flexBasis: 0 }}
            />
          ) : null}
        </div>
      </div>
    </div>
  );
}
