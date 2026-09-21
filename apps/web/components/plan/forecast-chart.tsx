"use client";

import { useId, useMemo, useState } from "react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { AXIS, ChartFrame, GRID, TOOLTIP_STYLE } from "@/components/ui/chart";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import type {
  BudgetScenario,
  ConfidenceBand,
  DemandForecastOutput,
  ScenarioName,
} from "@/lib/api/plan";
import { compactNumber, usd } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * The two figures the budget gate is argued with (Stage 02 PRD §15.3 C, D).
 *
 * Both are drawn from node output and nothing else, so what a budget owner sees
 * here is the arithmetic the plan will carry. Neither component computes a
 * forecast: §15.4 rule 2 calls a duplicated formula in the frontend a bug
 * rather than an optimisation, and the only arithmetic below turns a figure
 * into a pixel width.
 *
 * Both are deliberately single-series. Spend and conversions live on scales two
 * orders of magnitude apart, and drawing them against two y-axes on one plot
 * invents a correlation that is an artefact of where the axes were pinned. The
 * forecast offers them one at a time instead.
 */

const SCENARIO_LABEL: Record<ScenarioName, string> = {
  cautious: "Cautious",
  expected: "Expected",
  aggressive: "Aggressive",
};

function scenarioName(name: ScenarioName): string {
  return SCENARIO_LABEL[name] ?? name;
}

// ---------------------------------------------------------------------------
// Scenario comparison
// ---------------------------------------------------------------------------

/**
 * One scenario's headline, bar and totals.
 *
 * The bar is the comparison (PRD §15.3 D asks for scenario comparison bars) and
 * `share` is this envelope against the largest on offer, so the three lengths
 * are read against each other rather than against an invented ceiling.
 *
 * Colour carries **emphasis**, never size: the length already carries size, and
 * spending the one free channel on a value ramp would restate it. So one card
 * is accent and the others are quiet.
 */
function ScenarioCard({
  scenario,
  share,
  emphasis,
  badge,
}: {
  scenario: BudgetScenario;
  share: number;
  emphasis: boolean;
  badge?: string;
}) {
  return (
    <>
      {/* Wraps rather than competing for one line: three of these sit in a
          third of a 390px panel, where "Expected" and "Recommended" side by
          side clipped to "RECOM". */}
      <div className="flex flex-wrap items-baseline justify-between gap-x-2">
        <span className="text-sm font-medium text-fg">{scenarioName(scenario.name)}</span>
        {badge ? (
          <span className="shrink-0 text-[0.625rem] font-medium tracking-wide text-fg-muted uppercase">
            {badge}
          </span>
        ) : null}
      </div>

      <div>
        <p data-numeric className="text-[length:var(--text-lg)] leading-none text-fg">
          {usd(scenario.monthly_total_usd, 0)}
        </p>
        {/* Its own line. Inline, the unit was the first thing to be cut off
            when the figure reached six digits. */}
        <p className="mt-0.5 text-xs text-fg-subtle">per month</p>
        {/* A track one step off the surface, so the smallest envelope still
            reads as a measured fraction and not as an empty row. */}
        <div aria-hidden className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-border">
          <div
            className={cn(
              "h-full rounded-full transition-[width] duration-200 motion-reduce:transition-none",
              // The quiet fill is a *text* token, not the next step of the
              // track's own grey: `--border-strong` on `--border` is one step
              // apart, which is right for chrome and unreadable for a mark.
              // These two bars are the comparison — if they cannot be seen
              // against their track, the card has three numbers and no chart.
              emphasis ? "bg-accent" : "bg-fg-subtle",
            )}
            style={{ width: `${Math.max(share * 100, 2)}%` }}
          />
        </div>
      </div>

      <dl className="space-y-0.5 text-xs text-fg-muted">
        <Total label="Estimated conversions" value={compactNumber(scenario.est_conv)} unit="conv" />
        {scenario.est_cpa !== null ? (
          <Total label="Estimated cost per acquisition" value={usd(scenario.est_cpa, 0)} unit="CPA" />
        ) : null}
        {scenario.est_pipeline_usd !== null ? (
          <Total
            label="Estimated pipeline"
            // Compact, unlike every other figure on this card. Pipeline is the
            // one that reaches seven digits, and `$2346000` is a number nobody
            // reads — it is a magnitude here, not a figure anyone reconciles
            // against the split, so `$2.3M` is both shorter and more honest
            // about its precision.
            value={`$${compactNumber(scenario.est_pipeline_usd)}`}
            unit="pipeline"
          />
        ) : null}
      </dl>
    </>
  );
}

function Total({ label, value, unit }: { label: string; value: string; unit: string }) {
  return (
    <div className="flex gap-1">
      <dt className="sr-only">{label}</dt>
      <dd data-numeric>{value}</dd>
      <span aria-hidden>{unit}</span>
    </div>
  );
}

function widest(scenarios: BudgetScenario[]): number {
  return Math.max(...scenarios.map((scenario) => scenario.monthly_total_usd), 0);
}

const CARD = "flex flex-col gap-2 rounded-[var(--radius)] border p-3";

/**
 * Three envelopes, compared and chosen in the same control.
 *
 * The PRD asks for a scenario selector carrying each scenario's totals
 * (§15.3 C.1) and for scenario comparison bars (§15.3 D). They are one thing:
 * the bar is what makes cautious and aggressive comparable at a glance, and a
 * read-only chart beside a separate selector would print the same three numbers
 * twice.
 *
 * Real radio inputs rather than styled buttons — arrow-key movement, the roving
 * tab stop and the announced group name all come free, and none of them survive
 * being reimplemented.
 */
export function ScenarioSelector({
  scenarios,
  selected,
  recommended,
  onSelect,
  disabled = false,
}: {
  scenarios: BudgetScenario[];
  selected: ScenarioName;
  recommended?: ScenarioName;
  onSelect: (name: ScenarioName) => void;
  disabled?: boolean;
}) {
  const group = useId();
  const largest = widest(scenarios);

  return (
    <fieldset disabled={disabled}>
      <legend className="mb-1.5 text-xs font-medium tracking-wide text-fg-muted uppercase">
        Envelope
      </legend>
      <div className="grid gap-2 sm:grid-cols-3">
        {scenarios.map((scenario) => {
          const checked = scenario.name === selected;
          return (
            <label
              key={scenario.name}
              className={cn(
                CARD,
                "cursor-pointer bg-surface transition-colors hover:bg-surface-hover",
                "has-[:focus-visible]:outline has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-[var(--accent)]",
                checked ? "border-accent bg-accent-soft" : "border-border",
                // Not dimmed: a `viewer` is reading a decision, not waiting
                // for one to become available, and 60% opacity on the three
                // figures they came for reads as a screen mid-load.
                disabled && "cursor-default hover:bg-surface",
              )}
            >
              <input
                type="radio"
                name={group}
                value={scenario.name}
                checked={checked}
                onChange={() => onSelect(scenario.name)}
                className="sr-only"
              />
              <ScenarioCard
                scenario={scenario}
                share={largest > 0 ? scenario.monthly_total_usd / largest : 0}
                emphasis={checked}
                badge={recommended === scenario.name ? "Recommended" : undefined}
              />
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}

/** The same three cards with nothing to decide — node 2.2.3's own output. */
export function ScenarioComparison({
  scenarios,
  recommended,
  reason,
  className,
}: {
  scenarios: BudgetScenario[];
  recommended: ScenarioName;
  reason?: string;
  className?: string;
}) {
  const largest = widest(scenarios);
  return (
    <ChartFrame
      className={className}
      title="What each appetite buys"
      note={reason ? `Recommended: ${scenarioName(recommended)}. ${reason}` : undefined}
    >
      <ul className="grid gap-2 sm:grid-cols-3">
        {scenarios.map((scenario) => (
          <li
            key={scenario.name}
            className={cn(
              CARD,
              scenario.name === recommended ? "border-accent bg-accent-soft" : "bg-surface",
            )}
          >
            <ScenarioCard
              scenario={scenario}
              share={largest > 0 ? scenario.monthly_total_usd / largest : 0}
              emphasis={scenario.name === recommended}
              badge={scenario.name === recommended ? "Recommended" : undefined}
            />
          </li>
        ))}
      </ul>
    </ChartFrame>
  );
}

// ---------------------------------------------------------------------------
// The forecast over time
// ---------------------------------------------------------------------------

type Measure = "cost" | "conversions";

type Point = {
  month: string;
  value: number;
  /** `[low, high]` for the range area, or null when this measure carries no band. */
  band: [number, number] | null;
  cost_usd: number;
  conversions: number;
  clicks: number;
  cpa_usd: number | null;
};

/**
 * The plan's months, and the corridor the cost could land in.
 *
 * The band is the click-price range the forecast was built on, so it widens
 * **cost** and leaves volume alone: a cheaper click buys the same impressions
 * at a lower bill, not more conversions. That is why switching to conversions
 * drops the band rather than drawing a flat one — a band on a series it does
 * not apply to is decoration that reads as uncertainty.
 *
 * `confidence_band` is one pair of percentages for the forecast as a whole
 * (`forecast.demand_v1` derives it from the total), so the same corridor lands
 * on every month. The caption says so. A per-month corridor would need
 * per-month CPC ranges and the node emits none.
 */
export function ForecastLine({
  forecast,
  className,
}: {
  forecast: DemandForecastOutput;
  className?: string;
}) {
  const [measure, setMeasure] = useState<Measure>("cost");
  const band = forecast.confidence_band;
  const banded = measure === "cost" && band.basis === "cpc_range";

  const data = useMemo<Point[]>(
    () =>
      forecast.monthly_totals.map((row) => ({
        // Verbatim: `month` is a label the plan chose and may be `2027-01`,
        // `Jan` or `wave 2`. Parsing it as a date is how the third one becomes
        // "Invalid Date" on the axis.
        month: row.month,
        value: measure === "cost" ? row.cost_usd : row.conversions,
        band:
          measure === "cost" && band.basis === "cpc_range"
            ? [row.cost_usd * (1 + band.low_pct / 100), row.cost_usd * (1 + band.high_pct / 100)]
            : null,
        cost_usd: row.cost_usd,
        conversions: row.conversions,
        clicks: row.clicks,
        cpa_usd: row.cpa_usd,
      })),
    [forecast.monthly_totals, measure, band],
  );

  /**
   * Tooltip and table carry the exact figure; the axis carries the magnitude.
   *
   * `usd()` has no thousands separator on purpose (it is the reader's locale,
   * and this plan has a DE market), which leaves `$80000` on a tick in a 380px
   * panel — five digits nobody parses at a glance. Compact notation is both
   * shorter and unambiguous in every locale.
   */
  const axisFormat = (value: number) =>
    measure === "cost" ? `$${compactNumber(value)}` : compactNumber(value);

  return (
    <ChartFrame
      className={className}
      title={`Forecast by month — ${measure === "cost" ? "spend" : "conversions"}`}
      note={captionFor(measure, band, forecast.monthly_totals.length)}
      actions={<MeasureToggle measure={measure} onChange={setMeasure} />}
    >
      {/* The height covers the plot and the axis band beneath it, so the card
          never grows a nested scrollbar to reach its own tick labels. */}
      <ResponsiveContainer width="100%" height={260}>
        <ComposedChart data={data} margin={{ top: 8, right: 12, bottom: 4, left: 4 }}>
          {/* Solid hairline. A dashed grid on a chart that is entirely a
              projection reads as "this part is projected" and invites the
              question of which part. */}
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis
            dataKey="month"
            tick={AXIS}
            tickLine={false}
            axisLine={{ stroke: GRID }}
            interval="preserveStartEnd"
            minTickGap={16}
          />
          <YAxis tick={AXIS} tickLine={false} axisLine={false} width={48} tickFormatter={axisFormat} />
          <Tooltip
            {...TOOLTIP_STYLE}
            cursor={{ stroke: "var(--border-strong)", strokeWidth: 1 }}
            content={<ForecastTooltip measure={measure} banded={banded} />}
          />
          {banded ? (
            <Area
              dataKey="band"
              // A wash, not a block: the band is context for the line, and a
              // saturated fill would out-weigh the series it qualifies.
              fill="var(--accent)"
              fillOpacity={0.1}
              stroke="none"
              isAnimationActive={false}
              activeDot={false}
              name="Range"
            />
          ) : null}
          <Line
            dataKey="value"
            stroke="var(--accent)"
            strokeWidth={2}
            strokeLinecap="round"
            strokeLinejoin="round"
            dot={false}
            // 8px across, ringed in the surface colour so it stays legible
            // where it crosses the band edge.
            activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--surface-raised)" }}
            isAnimationActive={false}
            name={measure === "cost" ? "Spend" : "Conversions"}
          />
        </ComposedChart>
      </ResponsiveContainer>

      <ForecastTable forecast={forecast} />
    </ChartFrame>
  );
}

function captionFor(measure: Measure, band: ConfidenceBand, months: number): string {
  const span = `${months} month${months === 1 ? "" : "s"}`;
  if (measure === "conversions") {
    return `Forecast conversions across ${span}. The confidence band is a click-price range, which moves what the plan costs rather than how much it buys, so it is not drawn here.`;
  }
  if (band.basis !== "cpc_range") {
    return `Forecast spend across ${span}. No band: the demand was priced at a point estimate, with no click-price range to widen it.`;
  }
  return `Forecast spend across ${span}. The band is the click-price range, ${band.low_pct.toFixed(1)}% to +${band.high_pct.toFixed(1)}% — one corridor for the forecast as a whole, applied to every month.`;
}

function MeasureToggle({
  measure,
  onChange,
}: {
  measure: Measure;
  onChange: (next: Measure) => void;
}) {
  return (
    <div
      role="group"
      aria-label="What to plot"
      className="inline-flex rounded-full border bg-surface p-0.5 text-xs"
    >
      {(["cost", "conversions"] as const).map((option) => (
        <button
          key={option}
          type="button"
          onClick={() => onChange(option)}
          aria-pressed={measure === option}
          className={cn(
            "rounded-full px-2.5 py-1 font-medium transition-colors",
            measure === option ? "bg-accent text-accent-fg" : "text-fg-muted hover:text-fg",
          )}
        >
          {option === "cost" ? "Spend" : "Conversions"}
        </button>
      ))}
    </div>
  );
}

/**
 * Every figure the chart plots, and the two it does not.
 *
 * A tooltip is the wrong place for the only copy of a number — it cannot be
 * read by keyboard, printed or copied — so the months are also a table. Closed
 * by default: the chart answers the shape question and the table answers "what
 * exactly was March", and only one of those is asked first.
 */
function ForecastTable({ forecast }: { forecast: DemandForecastOutput }) {
  // A *named* group. `group-open:` compiles to a selector matching any open
  // `.group` ancestor, and this table lives inside another disclosure on the
  // budget gate card — unnamed, it announced "Hide the table" the moment that
  // outer disclosure was opened, while the table itself was still closed.
  return (
    <details className="group/months mt-3 border-t pt-3">
      <summary className="cursor-pointer list-none text-xs text-fg-muted hover:text-fg [&::-webkit-details-marker]:hidden">
        <span className="group-open/months:hidden">Show the months as a table</span>
        <span className="hidden group-open/months:inline">Hide the table</span>
      </summary>
      <div className="mt-2 max-h-72 overflow-y-auto">
        <Table label="The forecast, month by month">
          <thead>
            <Tr>
              <Th>Month</Th>
              <Th className="text-right">Clicks</Th>
              <Th className="text-right">Conversions</Th>
              <Th className="text-right">Spend</Th>
              <Th className="text-right">CPA</Th>
            </Tr>
          </thead>
          <tbody>
            {forecast.monthly_totals.map((row) => (
              <Tr key={row.month}>
                <Td className="font-mono text-xs">{row.month}</Td>
                <Td data-numeric className="text-right">
                  {compactNumber(row.clicks)}
                </Td>
                <Td data-numeric className="text-right">
                  {row.conversions.toFixed(1)}
                </Td>
                <Td data-numeric className="text-right">
                  {usd(row.cost_usd, 0)}
                </Td>
                <Td data-numeric className="text-right">
                  {row.cpa_usd === null ? "—" : usd(row.cpa_usd, 0)}
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      </div>
    </details>
  );
}

function ForecastTooltip({
  active,
  payload,
  measure,
  banded,
}: {
  active?: boolean;
  payload?: { payload: Point }[];
  measure: Measure;
  banded: boolean;
}) {
  const point = active ? payload?.[0]?.payload : undefined;
  if (!point) return null;
  return (
    <div className="rounded-[var(--radius)] border bg-surface-raised px-3 py-2 text-xs shadow-[var(--shadow-overlay)]">
      <p className="font-mono text-fg">{point.month}</p>
      <dl className="mt-1 space-y-0.5 text-fg-muted">
        <TipRow label="Spend" value={usd(point.cost_usd, 0)} strong={measure === "cost"} />
        {banded && point.band ? (
          <TipRow label="Range" value={`${usd(point.band[0], 0)} – ${usd(point.band[1], 0)}`} />
        ) : null}
        <TipRow
          label="Conversions"
          value={point.conversions.toFixed(1)}
          strong={measure === "conversions"}
        />
        <TipRow label="Clicks" value={compactNumber(point.clicks)} />
        <TipRow label="CPA" value={point.cpa_usd === null ? "—" : usd(point.cpa_usd, 0)} />
      </dl>
    </div>
  );
}

function TipRow({ label, value, strong }: { label: string; value: string; strong?: boolean }) {
  return (
    <div className="flex justify-between gap-4">
      <dt>{label}</dt>
      <dd data-numeric className={strong ? "font-medium text-fg" : undefined}>
        {value}
      </dd>
    </div>
  );
}
