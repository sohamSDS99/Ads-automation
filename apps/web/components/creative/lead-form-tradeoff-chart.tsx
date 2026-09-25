"use client";

import { useState } from "react";
import {
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { EvidenceIdsChip } from "@/components/creative/source-chip";
import { Badge } from "@/components/ui/badge";
import { AXIS, ChartFrame, ChartMissing, GRID, TOOLTIP_STYLE } from "@/components/ui/chart";
import { SegmentedControl } from "@/components/ui/segmented";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import type { LeadFormTradeoff, TradeoffOption } from "@/lib/api/creative-runs";
import { useEvidence } from "@/lib/queries";

const FORMULA = "leadform.field_tradeoff_v1";

/** Series in the palette's fixed order (tokens.css `--series-*`, validated for CVD). */
const SERIES = [
  { key: "expected_leads", label: "Expected leads", color: "var(--series-1)" },
  { key: "expected_qualified", label: "Expected qualified leads", color: "var(--series-2)" },
] as const;

type View = "chart" | "table";

/**
 * `LeadFormTradeoffChart` — how many fields the lead form should ask (Stage 04
 * PRD §15.4 F): fields on x, expected leads and expected qualified leads on y,
 * the point 4.3.3 chose marked, the calculation one click away.
 *
 * Every number is `leadform.field_tradeoff_v1`'s, read from the `derived`
 * evidence row the node cited (`result.options`), and the chosen point is the
 * node's `tradeoff` — this component draws, it never picks. Both series are
 * counts of leads, so they share one axis honestly. Identity is never colour
 * alone: a legend names both, each line is labelled where it ends, and the
 * table view holds every figure.
 */
export function LeadFormTradeoffChart({ tradeoff, projectId }: { tradeoff: LeadFormTradeoff; projectId: string }) {
  const [view, setView] = useState<View>("chart");
  const evidence = useEvidence({ project_id: projectId, ids: tradeoff.calc_evidence_ids, limit: 50 });
  const row = evidence.data?.pages
    .flatMap((page) => page.items)
    .find((item) => item.payload["formula_id"] === FORMULA);
  const result = row?.payload["result"] as { options?: TradeoffOption[] } | undefined;
  const options = [...(result?.options ?? [])].sort((a, b) => a.fields_n - b.fields_n);
  const chosen = options.find((option) => option.fields_n === tradeoff.fields_n) ?? null;
  const title = "Lead form length against qualified leads";
  const calc = (
    <EvidenceIdsChip
      label="calc"
      title={`Computed by calc/ (${FORMULA}) from the CRM's won and lost deals`}
      ids={tradeoff.calc_evidence_ids}
      projectId={projectId}
    />
  );

  if (evidence.isPending) return <Skeleton className="h-80 w-full" />;
  if (!chosen || options.length === 0) {
    return (
      <div className="flex flex-col gap-2">
        <ChartMissing
          title={title}
          reason={`The ${FORMULA} evidence row the lead form cites is no longer stored, so its curve cannot be drawn. The node chose ${tradeoff.fields_n} fields: ${tradeoff.expected_leads} expected leads, ${tradeoff.expected_qualified} qualified.`}
        />
        <p className="text-xs text-fg-muted">Cited calculation: {calc}</p>
      </div>
    );
  }

  return (
    <ChartFrame
      title={title}
      note={`Chosen: ${chosen.fields_n} fields, asking ${chosen.signals_asked} of the lead definition's signals — ${chosen.expected_leads} expected leads, ${chosen.expected_qualified} qualified.`}
      actions={
        <SegmentedControl<View>
          label="Show the trade-off as"
          value={view}
          onChange={setView}
          options={[
            { value: "chart", label: "Chart" },
            { value: "table", label: "Table" },
          ]}
        />
      }
    >
      <div className="mb-2 flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
        <ul aria-label="Series" className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-fg-muted">
          {SERIES.map((series) => (
            <li key={series.key} className="inline-flex items-center gap-1.5">
              <svg aria-hidden width="16" height="8" className="shrink-0">
                <line x1="0" y1="4" x2="16" y2="4" stroke={series.color} strokeWidth="2" strokeLinecap="round" />
                <circle cx="8" cy="4" r="3" fill={series.color} />
              </svg>
              {series.label}
            </li>
          ))}
        </ul>
        <p className="text-xs text-fg-muted">Calculation: {calc}</p>
      </div>

      {view === "chart" ? (
        <div data-testid="tradeoff-chart" data-chosen-fields={chosen.fields_n}>
          <ResponsiveContainer width="100%" height={260}>
            <ComposedChart data={options} margin={{ top: 16, right: 40, bottom: 4, left: 4 }}>
              <CartesianGrid stroke={GRID} vertical={false} />
              <XAxis
                type="number"
                dataKey="fields_n"
                domain={["dataMin", "dataMax"]}
                ticks={options.map((option) => option.fields_n)}
                allowDecimals={false}
                padding={{ left: 16, right: 16 }}
                tick={AXIS}
                tickLine={false}
                axisLine={{ stroke: GRID }}
                label={{ value: "Fields on the form", position: "insideBottomRight", offset: -2, ...AXIS, fill: "var(--fg-subtle)" }}
                height={36}
              />
              <YAxis tick={AXIS} tickLine={false} axisLine={false} width={44} allowDecimals={false} />
              <Tooltip
                {...TOOLTIP_STYLE}
                cursor={{ stroke: "var(--border-strong)", strokeWidth: 1 }}
                content={<TradeoffTooltip chosen={chosen.fields_n} />}
              />
              <ReferenceLine x={chosen.fields_n} stroke="var(--border-strong)" strokeDasharray="4 4" />
              {SERIES.map((series) => (
                <Line
                  key={series.key}
                  dataKey={series.key}
                  name={series.label}
                  stroke={series.color}
                  strokeWidth={2}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  isAnimationActive={false}
                  dot={(props: { cx?: number; cy?: number; index?: number; payload?: TradeoffOption }) => (
                    <circle
                      key={`${series.key}-${props.index}`}
                      cx={props.cx}
                      cy={props.cy}
                      r={4}
                      fill={series.color}
                      stroke="var(--surface-raised)"
                      strokeWidth={2}
                      data-series={series.key}
                      data-fields={props.payload?.fields_n}
                    />
                  )}
                  activeDot={{ r: 5, strokeWidth: 2, stroke: "var(--surface-raised)" }}
                  label={(props: { x?: number | string; y?: number | string; index?: number }) =>
                    props.index === options.length - 1 ? (
                      <text
                        key={`${series.key}-end`}
                        x={Number(props.x ?? 0) + 8}
                        y={Number(props.y ?? 0) + 4}
                        fontSize={11}
                        fill="var(--fg-muted)"
                      >
                        {series.key === "expected_leads" ? "Leads" : "Qualified"}
                      </text>
                    ) : (
                      <g key={`${series.key}-${props.index}`} />
                    )
                  }
                />
              ))}
              <ReferenceDot
                x={chosen.fields_n}
                y={chosen.expected_qualified}
                ifOverflow="visible"
                shape={(props: { cx?: number; cy?: number }) => (
                  <g data-testid="tradeoff-chosen" data-fields={chosen.fields_n} data-cx={props.cx} data-cy={props.cy}>
                    <circle cx={props.cx} cy={props.cy} r={8} fill="none" stroke="var(--fg)" strokeWidth={2} />
                    <text
                      x={props.cx}
                      y={(props.cy ?? 0) - 14}
                      textAnchor="middle"
                      fontSize={11}
                      fontWeight={500}
                      fill="var(--fg)"
                    >
                      Chosen
                    </text>
                  </g>
                )}
              />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <Table label="Expected leads by lead form length">
          <thead>
            <tr>
              <Th className="text-right">Fields</Th>
              <Th className="text-right">Signals asked</Th>
              <Th className="text-right">Expected leads</Th>
              <Th className="text-right">Junk rate</Th>
              <Th className="text-right">Expected qualified</Th>
              <Th>
                <span className="sr-only">Chosen</span>
              </Th>
            </tr>
          </thead>
          <tbody>
            {options.map((option) => (
              <Tr key={option.fields_n}>
                <Td className="text-right tabular-nums">{option.fields_n}</Td>
                <Td className="text-right tabular-nums">{option.signals_asked}</Td>
                <Td className="text-right tabular-nums">{option.expected_leads}</Td>
                <Td className="text-right tabular-nums">{(option.junk_rate * 100).toFixed(1)}%</Td>
                <Td className="text-right tabular-nums">{option.expected_qualified}</Td>
                <Td>{option.fields_n === chosen.fields_n ? <Badge tone="accent">Chosen</Badge> : null}</Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}
    </ChartFrame>
  );
}

function TradeoffTooltip({
  active,
  payload,
  chosen,
}: {
  active?: boolean;
  payload?: { payload: TradeoffOption }[];
  chosen: number;
}) {
  const option = payload?.[0]?.payload;
  if (!active || !option) return null;
  return (
    <div style={TOOLTIP_STYLE.contentStyle} className="px-2.5 py-2">
      <p className="mb-1 text-fg-muted">
        {option.fields_n} fields · {option.signals_asked} signals asked
        {option.fields_n === chosen ? " · chosen" : ""}
      </p>
      <dl className="grid grid-cols-2 gap-x-3 tabular-nums">
        <dt>Expected leads</dt>
        <dd className="text-right">{option.expected_leads}</dd>
        <dt>Qualified</dt>
        <dd className="text-right">{option.expected_qualified}</dd>
        <dt>Junk rate</dt>
        <dd className="text-right">{(option.junk_rate * 100).toFixed(1)}%</dd>
      </dl>
    </div>
  );
}
