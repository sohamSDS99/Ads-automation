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
const AXIS = { stroke: "var(--fg-subtle)", fontSize: 11 };
const GRID = "var(--border)";

/** Intent is categorical, so it gets its own small scale, not the status one. */
const INTENT_COLOR: Record<string, string> = {
  transactional: "#2563eb",
  commercial_investigation: "#0891b2",
  informational: "#7c3aed",
  navigational: "#0d9488",
  irrelevant: "#a1a1aa",
};

export function ChartFrame({
  title,
  note,
  children,
  className,
}: {
  title: string;
  note?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <figure className={cn("rounded-[var(--radius)] border bg-surface-raised p-4", className)}>
      <figcaption className="mb-3">
        <h4 className="text-sm font-medium text-fg">{title}</h4>
        {note ? <p className="mt-0.5 text-xs text-fg-subtle">{note}</p> : null}
      </figcaption>
      {children}
    </figure>
  );
}

const TOOLTIP_STYLE = {
  contentStyle: {
    background: "var(--surface-raised)",
    border: "1px solid var(--border)",
    borderRadius: "var(--radius)",
    fontSize: "0.75rem",
    color: "var(--fg)",
  },
  labelStyle: { color: "var(--fg-muted)" },
} as const;

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

/** Demand against price, coloured by what the searcher is trying to do. */
export function KeywordChart({ keywords }: { keywords: PricedKeyword[] }) {
  const data = useMemo(
    () =>
      keywords
        .filter((keyword) => keyword.volume !== null && keyword.volume !== undefined)
        .map((keyword) => ({
          term: keyword.term,
          volume: keyword.volume ?? 0,
          cpc: midCpc(keyword),
          intent: keyword.intent ?? "unclassified",
        })),
    [keywords],
  );
  if (data.length === 0) return null;
  const intents = [...new Set(data.map((row) => row.intent))];

  return (
    <ChartFrame
      title="Demand against price"
      note="Monthly searches by mid-range click cost. Colour is the intent behind the search."
    >
      <ResponsiveContainer width="100%" height={240}>
        <ScatterChart margin={{ top: 8, right: 8, bottom: 8, left: 8 }}>
          <CartesianGrid stroke={GRID} strokeDasharray="3 3" />
          <XAxis
            type="number"
            dataKey="volume"
            name="Searches"
            tick={AXIS}
            tickLine={false}
            axisLine={{ stroke: GRID }}
            tickFormatter={(value: number) => compactNumber(value)}
          />
          <YAxis
            type="number"
            dataKey="cpc"
            name="CPC"
            tick={AXIS}
            tickLine={false}
            axisLine={{ stroke: GRID }}
            tickFormatter={(value: number) => usd(value, 2)}
            width={52}
          />
          <ZAxis range={[50, 50]} />
          <Tooltip {...TOOLTIP_STYLE} cursor={{ strokeDasharray: "3 3", stroke: "var(--border-strong)" }} />
          {intents.map((intent) => (
            <Scatter
              key={intent}
              name={INTENT_LABEL[intent as Intent] ?? intent}
              data={data.filter((row) => row.intent === intent)}
              fill={INTENT_COLOR[intent] ?? "var(--status-skipped)"}
            />
          ))}
          <Legend iconSize={8} wrapperStyle={{ fontSize: "0.75rem", color: "var(--fg-muted)" }} />
        </ScatterChart>
      </ResponsiveContainer>
    </ChartFrame>
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
  const highest = Math.max(...months.map((value) => value ?? 0), 1);

  return (
    <ChartFrame
      title="When the demand is"
      note="Search interest by month, averaged across the keyword list and weighted by volume."
    >
      <ol className="grid grid-cols-12 gap-1">
        {months.map((value, index) => {
          const share = value === null ? 0 : value / highest;
          return (
            <li key={MONTH_NAMES[index]} className="flex flex-col items-center gap-1">
              <span
                title={`${MONTH_NAMES[index]}: ${value === null ? "no data" : value.toFixed(2)}`}
                className="block h-14 w-full rounded-[4px] border"
                style={{
                  // One hue, one dimension: opacity. A rainbow here would imply
                  // categories where there is only more and less.
                  backgroundColor:
                    value === null ? "var(--surface)" : `color-mix(in srgb, var(--accent) ${Math.round(share * 100)}%, var(--surface))`,
                }}
              />
              <span className="text-[0.625rem] text-fg-subtle">{MONTHS[index]}</span>
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
