"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";
import { useMemo } from "react";

import { AXIS, ChartFrame, GRID, TOOLTIP_STYLE } from "@/components/ui/chart";
import {
  INTENT_LABEL,
  type Intent,
  type MessageCluster,
  type PricedKeyword,
  type ProfitableTerm,
  type WastefulTerm,
} from "@/lib/api/reports";
import { compactNumber, usd } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * The report's four figures (PRD §13.4 C).
 *
 * Every one of them is drawn from `ResearchReport` and nothing else, so what a
 * chart shows is exactly what the PDF contains. Where the PRD asks for a shape
 * the contract cannot supply — a spend-versus-conversions *timeline*, which
 * would need a monthly series no node emits — the same question is answered
 * with the data that does exist, and the axis is labelled for what it is.
 */

/** Intent is categorical, so it gets its own small scale, not the status one. */
const INTENT_COLOR: Record<string, string> = {
  transactional: "#2563eb",
  commercial_investigation: "#0891b2",
  informational: "#7c3aed",
  navigational: "#0d9488",
  irrelevant: "#a1a1aa",
  unclassified: "#f59e0b",
};

/**
 * The intents worth plotting.
 *
 * `irrelevant` is excluded on purpose rather than drawn in grey. Half of a
 * vendor keyword pull is scrape debris — two-letter fragments carrying
 * six-figure volumes — and one of those points stretches a linear axis so far
 * that every real term collapses into a stripe against the Y axis. The figure
 * is meant to be read, so the terms the classifier already judged irrelevant do
 * not get to set its scale. The count that was dropped is printed under the
 * title, because a filter nobody is told about is a lie.
 */
const PLOTTED_INTENTS = new Set([
  "transactional",
  "commercial_investigation",
  "informational",
  "navigational",
  "unclassified",
]);

/** Cost against conversions, per search term the account already ran. */
export function SpendChart({
  profitable,
  wasteful,
}: {
  profitable: ProfitableTerm[];
  wasteful: WastefulTerm[];
}) {
  const data = useMemo(
    () => [
      ...profitable.map((term) => ({
        term: term.term,
        cost: term.cost ?? 0,
        conv: term.conv ?? 0,
        kind: "Converting" as const,
      })),
      ...wasteful.map((term) => ({
        term: term.term,
        cost: term.cost ?? 0,
        conv: term.conv ?? 0,
        kind: "Wasted" as const,
      })),
    ],
    [profitable, wasteful],
  );
  if (data.length === 0) return null;

  return (
    <ChartFrame
      title="Where the money went"
      note="Every search term the account has paid for, by cost and the conversions it produced."
    >
      <ResponsiveContainer width="100%" height={240}>
        <ScatterChart margin={{ top: 8, right: 8, bottom: 8, left: 8 }}>
          <CartesianGrid stroke={GRID} strokeDasharray="3 3" />
          <XAxis
            type="number"
            dataKey="cost"
            name="Cost"
            tick={AXIS}
            tickLine={false}
            axisLine={{ stroke: GRID }}
            tickFormatter={(value: number) => usd(value, 0)}
          />
          <YAxis
            type="number"
            dataKey="conv"
            name="Conversions"
            tick={AXIS}
            tickLine={false}
            axisLine={{ stroke: GRID }}
            width={40}
          />
          <ZAxis range={[60, 60]} />
          <Tooltip
            {...TOOLTIP_STYLE}
            cursor={{ strokeDasharray: "3 3", stroke: "var(--border-strong)" }}
          />
          <Scatter name="Converting" data={data.filter((row) => row.kind === "Converting")} fill="var(--status-success)" />
          <Scatter name="Wasted" data={data.filter((row) => row.kind === "Wasted")} fill="var(--status-failed)" />
          <Legend iconSize={8} wrapperStyle={{ fontSize: "0.75rem", color: "var(--fg-muted)" }} />
        </ScatterChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}

/**
 * Ticks at the decades a log axis is actually read in.
 *
 * Recharts derives its own ticks from the data when a log scale is in play, and
 * on a cloud of 500 keywords that produces a tick per cluster — "90 1K 1.1K
 * 1.4K 1.7K 2K" overprinted into an unreadable smear along the axis. A log axis
 * only ever wants its powers of ten (with a 3x midpoint while the range is
 * short), so they are supplied explicitly.
 */
function decadeTicks(lowest: number, highest: number): number[] {
  if (!Number.isFinite(lowest) || !Number.isFinite(highest) || lowest <= 0) return [];
  const first = Math.floor(Math.log10(lowest));
  const last = Math.ceil(Math.log10(highest));
  const dense = last - first <= 3;
  const ticks: number[] = [];
  for (let power = first; power <= last; power += 1) {
    const decade = 10 ** power;
    if (decade >= lowest * 0.9 && decade <= highest * 1.1) ticks.push(decade);
    if (dense) {
      const mid = decade * 3;
      if (mid >= lowest * 0.9 && mid <= highest * 1.1) ticks.push(mid);
    }
  }
  return ticks.sort((a, b) => a - b);
}

/**
 * Demand against price, coloured by what the searcher is trying to do.
 *
 * Both axes are logarithmic. Keyword volume is not a linear quantity — the list
 * runs from 10 searches a month to 50,000 and the interesting cluster sits at
 * the bottom of that range, so a linear axis spends 95% of its width on empty
 * space and stacks everything readable on the origin. Log spacing gives each
 * order of magnitude the same room, which is how the figure is actually read:
 * "cheap and popular" is a corner, not a pixel.
 */
export function KeywordChart({ keywords }: { keywords: PricedKeyword[] }) {
  const { data, dropped } = useMemo(() => {
    const priced = keywords.filter(
      (keyword) => keyword.volume !== null && keyword.volume !== undefined,
    );
    const rows = priced
      .map((keyword) => ({
        term: keyword.term,
        volume: keyword.volume ?? 0,
        cpc: midCpc(keyword),
        intent: keyword.intent ?? "unclassified",
      }))
      // A log axis has no room for zero, and a term with no price is not a
      // point on a price chart — it is a gap in the data, counted below.
      .filter((row) => row.volume > 0 && row.cpc > 0 && PLOTTED_INTENTS.has(row.intent));
    return { data: rows, dropped: priced.length - rows.length };
  }, [keywords]);

  if (data.length === 0) return null;
  const intents = [...new Set(data.map((row) => row.intent))].sort();
  const volumes = data.map((row) => row.volume);
  const costs = data.map((row) => row.cpc);
  const volumeTicks = decadeTicks(Math.min(...volumes), Math.max(...volumes));
  const costTicks = decadeTicks(Math.min(...costs), Math.max(...costs));

  return (
    <ChartFrame
      title="Demand against price"
      note={
        `Monthly searches against mid-range click cost, both on a log scale. Colour is the intent behind the search.` +
        (dropped > 0
          ? ` ${dropped.toLocaleString()} of ${(data.length + dropped).toLocaleString()} terms are not plotted: they were classified irrelevant, or carry no volume or no price.`
          : "")
      }
    >
      <ResponsiveContainer width="100%" height={300}>
        <ScatterChart margin={{ top: 8, right: 16, bottom: 28, left: 20 }}>
          <CartesianGrid stroke={GRID} strokeDasharray="3 3" />
          <XAxis
            type="number"
            dataKey="volume"
            name="Searches"
            scale="log"
            domain={["auto", "auto"]}
            allowDataOverflow
            ticks={volumeTicks.length > 1 ? volumeTicks : undefined}
            tick={AXIS}
            tickLine={false}
            axisLine={{ stroke: GRID }}
            tickFormatter={(value: number) => compactNumber(value)}
            label={{
              value: "Monthly searches",
              position: "insideBottom",
              offset: -16,
              fill: "var(--fg-subtle)",
              fontSize: 11,
            }}
          />
          <YAxis
            type="number"
            dataKey="cpc"
            name="CPC"
            scale="log"
            domain={["auto", "auto"]}
            allowDataOverflow
            ticks={costTicks.length > 1 ? costTicks : undefined}
            tick={AXIS}
            tickLine={false}
            axisLine={{ stroke: GRID }}
            tickFormatter={(value: number) => usd(value, 2)}
            width={60}
            label={{
              value: "Cost per click",
              angle: -90,
              position: "insideLeft",
              offset: -8,
              fill: "var(--fg-subtle)",
              fontSize: 11,
            }}
          />
          <ZAxis range={[46, 46]} />
          <Tooltip
            {...TOOLTIP_STYLE}
            cursor={{ strokeDasharray: "3 3", stroke: "var(--border-strong)" }}
            content={<KeywordTooltip />}
          />
          {intents.map((intent) => (
            <Scatter
              key={intent}
              name={INTENT_LABEL[intent as Intent] ?? intent}
              data={data.filter((row) => row.intent === intent)}
              fill={INTENT_COLOR[intent] ?? "var(--status-skipped)"}
              fillOpacity={0.75}
            />
          ))}
          <Legend
            iconSize={8}
            verticalAlign="top"
            align="right"
            wrapperStyle={{ fontSize: "0.75rem", color: "var(--fg-muted)" }}
          />
        </ScatterChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}

/**
 * The term, not just its coordinates.
 *
 * Recharts' default tooltip lists the numeric keys it plotted, which on this
 * figure reads "Searches 2,900 / CPC 3.68" and never says *which keyword* —
 * the one thing a reader hovers a point to find out.
 */
function KeywordTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: { payload: { term: string; volume: number; cpc: number; intent: string } }[];
}) {
  const row = active ? payload?.[0]?.payload : undefined;
  if (!row) return null;
  return (
    <div className="rounded-[var(--radius)] border bg-surface-raised px-3 py-2 text-xs shadow-sm">
      <p className="font-mono text-fg">{row.term}</p>
      <p className="mt-1 text-fg-muted">
        {compactNumber(row.volume)} searches · {usd(row.cpc, 2)} per click
      </p>
      <p className="text-fg-subtle">{INTENT_LABEL[row.intent as Intent] ?? row.intent}</p>
    </div>
  );
}

/** What competitors say, by how many of them say it. */
export function MessageChart({ clusters }: { clusters: MessageCluster[] }) {
  const data = useMemo(
    () =>
      clusters
        .map((cluster) => ({
          theme: cluster.theme,
          frequency: cluster.frequency ?? cluster.advertisers?.length ?? 0,
        }))
        .sort((a, b) => b.frequency - a.frequency)
        .slice(0, 10),
    [clusters],
  );
  if (data.length === 0) return null;

  return (
    <ChartFrame
      title="What competitors are saying"
      note="The ten most repeated messages across the creatives we collected."
    >
      <ResponsiveContainer width="100%" height={Math.max(180, data.length * 30)}>
        <BarChart data={data} layout="vertical" margin={{ top: 4, right: 16, bottom: 4, left: 8 }}>
          <CartesianGrid stroke={GRID} strokeDasharray="3 3" horizontal={false} />
          <XAxis type="number" tick={AXIS} tickLine={false} axisLine={{ stroke: GRID }} allowDecimals={false} />
          <YAxis
            type="category"
            dataKey="theme"
            tick={AXIS}
            tickLine={false}
            axisLine={{ stroke: GRID }}
            width={160}
          />
          <Tooltip {...TOOLTIP_STYLE} cursor={{ fill: "var(--surface-hover)" }} />
          <Bar dataKey="frequency" radius={[0, 4, 4, 0]} fill="var(--accent)">
            {data.map((row) => (
              <Cell key={row.theme} fill="var(--accent)" />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}

const MONTHS = ["J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"];
const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

/**
 * When the demand actually is.
 *
 * A grid rather than a charting component: twelve cells with one value each is
 * geometry, and drawing it directly gives every cell a real title and a real
 * contrast ratio.
 *
 * The shade is stretched across the *observed* range rather than from zero.
 * Seasonality indices cluster tightly — a real year might run 0.88 to 1.14 —
 * and mapping that onto 0-100% opacity paints twelve cells the same blue, which
 * says "flat" when the truth is a 30% swing between the best month and the
 * worst. Anchoring the pale end at the quietest month and the solid end at the
 * busiest is what makes the shape visible. Because that rescaling would flatter
 * a genuinely flat year into looking seasonal, the index is printed under every
 * month and the peak is named in the caption.
 */
export function SeasonalityChart({ keywords }: { keywords: PricedKeyword[] }) {
  const months = useMemo(() => {
    const totals = Array.from({ length: 12 }, () => ({ sum: 0, count: 0 }));
    for (const keyword of keywords) {
      const series = keyword.seasonality_index ?? [];
      if (series.length !== 12) continue;
      const weight = keyword.volume ?? 1;
      series.forEach((value, index) => {
        totals[index]!.sum += value * weight;
        totals[index]!.count += weight;
      });
    }
    return totals.map((total) => (total.count === 0 ? null : total.sum / total.count));
  }, [keywords]);

  if (months.every((value) => value === null)) return null;

  const known = months.filter((value): value is number => value !== null);
  const highest = Math.max(...known);
  const lowest = Math.min(...known);
  const spread = highest - lowest;
  const peak = months.indexOf(highest);
  const trough = months.indexOf(lowest);
  // Zero spread means every month is identical; a flat scale is then the honest
  // rendering, and `share` stays at the midpoint rather than dividing by zero.
  const shareOf = (value: number) => (spread === 0 ? 0.5 : (value - lowest) / spread);

  return (
    <ChartFrame
      title="When the demand is"
      note={
        spread === 0
          ? "Search interest by month, averaged across the keyword list and weighted by volume. Demand is flat across the year."
          : `Search interest by month, averaged across the keyword list and weighted by volume. Busiest is ${MONTH_NAMES[peak]} at ${highest.toFixed(2)}, quietest is ${MONTH_NAMES[trough]} at ${lowest.toFixed(2)} — shading runs between those two, not from zero.`
      }
    >
      <ol className="grid grid-cols-12 gap-1">
        {months.map((value, index) => {
          // Floor the pale end at 12% so the quietest month still reads as a
          // filled cell rather than a hole in the row.
          const share = value === null ? 0 : 0.12 + shareOf(value) * 0.88;
          const isPeak = value !== null && index === peak && spread > 0;
          return (
            <li key={MONTH_NAMES[index]} className="flex flex-col items-center gap-1">
              <span
                title={`${MONTH_NAMES[index]}: ${value === null ? "no data" : value.toFixed(2)}`}
                className={cn(
                  "block h-16 w-full rounded-[4px] border",
                  isPeak && "ring-2 ring-accent ring-offset-1 ring-offset-[var(--surface-raised)]",
                )}
                style={{
                  // One hue, one dimension: opacity. A rainbow here would imply
                  // categories where there is only more and less.
                  backgroundColor:
                    value === null
                      ? "var(--surface)"
                      : `color-mix(in srgb, var(--accent) ${Math.round(share * 100)}%, var(--surface))`,
                }}
              />
              <span className="text-[0.625rem] leading-none text-fg-subtle">{MONTHS[index]}</span>
              <span
                data-numeric
                className={cn(
                  "text-[0.625rem] leading-none tabular-nums",
                  isPeak ? "font-medium text-fg" : "text-fg-subtle",
                )}
              >
                {value === null ? "—" : value.toFixed(2)}
              </span>
            </li>
          );
        })}
      </ol>
    </ChartFrame>
  );
}

function midCpc(keyword: PricedKeyword): number {
  const low = keyword.cpc_low ?? 0;
  const high = keyword.cpc_high ?? low;
  return (low + high) / 2;
}
