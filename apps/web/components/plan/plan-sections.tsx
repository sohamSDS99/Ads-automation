"use client";

/**
 * The sections of the Plan Viewer that are not the tree (Stage 02 PRD §15.3 D).
 *
 * Every one of them reads a branch of the §12 payload and renders an honest
 * absence when it is not there. That is not defensive coding for its own sake:
 * node 2.6.1 fills the payload, a plan run can halt at a gate before reaching
 * it, and a section that throws on a partial plan takes the whole viewer down
 * with it — including the gate decisions somebody opened the page to read.
 *
 * The figures come through `<Figure>`, which draws the calculation chip when a
 * value arrived as a §12 `Number` and stays quiet when it arrived as a plain
 * number. No section formats money or percentages itself.
 */

import { ArrowRight } from "lucide-react";

import { Figure, formatFigure, type CalcIndex } from "@/components/plan/figure";
import { ForecastLine, ScenarioComparison } from "@/components/plan/forecast-chart";
import { Alert } from "@/components/ui/alert";
import { ChartFrame } from "@/components/ui/chart";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { Tooltip } from "@/components/ui/tooltip";
import {
  FIGURE_PATHS,
  figureValue,
  type BudgetScenario,
  type PlanFigure,
  type ScenarioName,
} from "@/lib/api/plan";
import { usd } from "@/lib/format";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// reading an opaque payload without pretending to know its shape
// ---------------------------------------------------------------------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** A dotted path into the payload, `undefined` rather than a throw on a gap. */
export function at(payload: unknown, path: string): unknown {
  let cursor: unknown = payload;
  for (const part of path.split(".")) {
    if (!isRecord(cursor)) return undefined;
    cursor = cursor[part];
  }
  return cursor;
}

/**
 * The first of several paths that answers.
 *
 * `export/plan_contract.py` names §12's fields and the PRD's sketch did not, so
 * every headline figure is read from the contract's spelling first and the node
 * output's second. A reader that insists on one spelling goes blank on the next
 * rename, and a blank figure is indistinguishable from an absent one.
 */
export function firstAt(payload: unknown, paths: readonly string[]): unknown {
  for (const path of paths) {
    const value = at(payload, path);
    if (value !== undefined && value !== null) return value;
  }
  return undefined;
}

export function rowsAt(payload: unknown, path: string): Record<string, unknown>[] {
  const value = at(payload, path);
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

export function textAt(payload: unknown, path: string): string | null {
  const value = at(payload, path);
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function figure(value: unknown): PlanFigure {
  if (typeof value === "number" || typeof value === "string") return value;
  return isRecord(value) && typeof value.value === "number" ? (value as never) : null;
}

/** What a section says when the plan has not produced it yet. */
export function SectionMissing({ what, why }: { what: string; why: string }) {
  return (
    <div className="rounded-[var(--radius)] border border-dashed bg-surface p-4">
      <p className="text-sm font-medium text-fg-muted">{what}</p>
      <p className="mt-0.5 text-xs text-fg-subtle">{why}</p>
    </div>
  );
}

type SectionProps = { payload: unknown; calcs: CalcIndex; projectId: string };

// ---------------------------------------------------------------------------
// objectives
// ---------------------------------------------------------------------------

/**
 * Target per campaign, with 2.1.2's ceiling beside it.
 *
 * §15.3 D: "the ceiling from 2.1.2 shown alongside so an over-ambitious target
 * is visible rather than buried". So the ceiling is a column, not a footnote,
 * and a target above it is called out on its own row — the reader is deciding
 * whether to sign off a number, and the number being unaffordable is the single
 * most useful thing this table can tell them.
 */
export function ObjectivesSection({ payload, calcs, projectId }: SectionProps) {
  const targets = rowsAt(payload, "objectives.campaign_targets");
  const blended = figure(firstAt(payload, FIGURE_PATHS.blendedTarget));
  const ceiling = figure(firstAt(payload, FIGURE_PATHS.blendedCeiling));
  const northStar = figure(firstAt(payload, FIGURE_PATHS.northStar));
  const lead = at(payload, "objectives.lead_definition");
  const kpis = rowsAt(payload, "objectives.kpis");

  if (targets.length === 0 && !blended) {
    return (
      <SectionMissing
        what="No objectives yet"
        why="Nodes 2.1.1 to 2.1.4 set the targets, and gate G1 approves them."
      />
    );
  }

  const over = targets.filter((row) => row.over_ceiling === true);

  return (
    <div className="space-y-4">
      <dl className="flex flex-wrap gap-x-8 gap-y-2">
        <Headline label="Blended target CPA">
          <Figure
            value={blended}
            unit="usd"
            calcs={calcs}
            projectId={projectId}
            label="Blended target CPA"
          />
        </Headline>
        <Headline label="Ceiling — the most we can pay">
          <Figure
            value={ceiling}
            unit="usd"
            calcs={calcs}
            projectId={projectId}
            label="Max cost per won deal"
          />
        </Headline>
        {northStar ? (
          <Headline label="North-star target">
            <Figure
              value={northStar}
              calcs={calcs}
              projectId={projectId}
              label="North-star target"
            />
          </Headline>
        ) : null}
      </dl>

      {over.length ? (
        <Alert tone="warning" title="A target sits above its own ceiling">
          {over.map((row) => str(row.campaign_ref)).filter(Boolean).join(", ")} —{" "}
          {over.length === 1 ? "this campaign is" : "these campaigns are"} planned to pay more per
          customer than the economics allow. Approving it commits to a loss on every conversion.
        </Alert>
      ) : null}

      {targets.length ? (
        <div className="overflow-x-auto">
          <Table label="Target per campaign, against its ceiling">
            <thead>
              <Tr>
                <Th>Campaign</Th>
                <Th>Market</Th>
                <Th>Metric</Th>
                <Th className="text-right">Target</Th>
                <Th className="text-right">Ceiling</Th>
              </Tr>
            </thead>
            <tbody>
              {targets.map((row, index) => (
                <Tr key={`${str(row.campaign_ref) ?? index}-${str(row.market) ?? ""}`}>
                  <Td className="font-mono text-xs">{str(row.campaign_ref) ?? "—"}</Td>
                  <Td>{str(row.market) ?? "—"}</Td>
                  <Td className="text-fg-muted">
                    {(str(row.metric) ?? "—").replace(/_/g, " ")}
                  </Td>
                  <Td className="text-right">
                    <span
                      className={cn(
                        "inline-flex",
                        row.over_ceiling === true && "text-status-gate",
                      )}
                    >
                      <Figure
                        value={figure(row.target)}
                        unit="usd"
                        calcs={calcs}
                        projectId={projectId}
                        label={`Target for ${str(row.campaign_ref) ?? "this campaign"}`}
                      />
                    </span>
                  </Td>
                  <Td className="text-right">
                    <Figure
                      value={figure(row.ceiling)}
                      unit="usd"
                      calcs={calcs}
                      projectId={projectId}
                      label={`Ceiling for ${str(row.campaign_ref) ?? "this campaign"}`}
                    />
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </div>
      ) : null}

      {kpis.length ? (
        <dl className="flex flex-wrap gap-x-8 gap-y-2 border-t pt-3">
          {kpis.map((row, index) => (
            <Headline key={str(row.name) ?? index} label={str(row.name) ?? "KPI"}>
              <Figure
                value={figure(row.target)}
                calcs={calcs}
                projectId={projectId}
                label={str(row.name) ?? "KPI"}
              />
            </Headline>
          ))}
        </dl>
      ) : null}

      {isRecord(lead) ? (
        <div className="border-t pt-3">
          <h4 className="text-sm font-medium text-fg">What counts as a lead</h4>
          <p className="mt-1 text-sm text-fg-muted">{str(lead.qualified_as) ?? "—"}</p>
          {Array.isArray(lead.scoring) && lead.scoring.length ? (
            <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-fg-muted">
              {lead.scoring.filter(isRecord).map((signal, index) => (
                <li key={str(signal.signal) ?? index}>
                  {(str(signal.signal) ?? "—").replace(/_/g, " ")}{" "}
                  <span data-numeric className="text-fg-subtle">
                    {formatFigure(figure(signal.weight), "ratio")}
                  </span>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function Headline({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-fg-muted">{label}</dt>
      <dd className="mt-0.5 text-[length:var(--text-lg)] leading-none">{children}</dd>
    </div>
  );
}

// ---------------------------------------------------------------------------
// media plan
// ---------------------------------------------------------------------------

/**
 * The envelope, the three appetites, the split and the twelve months.
 *
 * Every figure here was computed by `calc/` and is re-rendered, never
 * recomputed: §15.4 rule 2 calls a duplicated formula in the frontend a bug
 * rather than an optimisation. The one exception is the allocation bar's pixel
 * width, which is a share of a total and not a forecast.
 */
export function MediaPlanSection({ payload, calcs, projectId }: SectionProps) {
  const envelope = at(payload, "media_plan.envelope");
  const allocation = rowsAt(payload, "media_plan.allocation");
  const scenarios = (at(payload, "media_plan.scenarios") ?? []) as BudgetScenario[];
  const monthly = rowsAt(payload, "media_plan.monthly_totals");
  const band = at(payload, "media_plan.confidence_band");
  const rules = at(payload, "media_plan.reallocation_rules");
  const chosen = textAt(payload, "media_plan.selected_scenario");
  const recommended = textAt(payload, "media_plan.recommended_scenario");

  if (!isRecord(envelope) && allocation.length === 0) {
    return (
      <SectionMissing
        what="No media plan yet"
        why="Stage 2.2 forecasts demand and builds the three scenarios; gate G3 approves the split."
      />
    );
  }

  const monthlyCap = figure(firstAt(payload, FIGURE_PATHS.monthlyEnvelope));
  const quarterlyCap = figure(firstAt(payload, FIGURE_PATHS.quarterlyEnvelope));
  const reserve = figure(firstAt(payload, FIGURE_PATHS.experimentReserve));
  // `figureValue`, not `typeof === "number"`: §12 says a figure in the plan is
  // a `Number` object, so the envelope arriving traced — which is the shape the
  // contract actually promises — would otherwise make every share an em dash.
  const capValue = figureValue(monthlyCap);

  return (
    <div className="space-y-4">
      <dl className="flex flex-wrap gap-x-8 gap-y-2">
        <Headline label="Monthly envelope">
          <Figure
            value={monthlyCap}
            unit="usd"
            calcs={calcs}
            projectId={projectId}
            label="Monthly envelope"
          />
        </Headline>
        <Headline label="Quarterly envelope">
          <Figure
            value={quarterlyCap}
            unit="usd"
            calcs={calcs}
            projectId={projectId}
            label="Quarterly envelope"
          />
        </Headline>
        {reserve ? (
          <Headline label="Experiment reserve">
            <Figure
              value={reserve}
              unit="usd"
              calcs={calcs}
              projectId={projectId}
              label="Experiment reserve"
            />
          </Headline>
        ) : null}
        {chosen ? (
          <div>
            <dt className="text-xs text-fg-muted">Appetite chosen</dt>
            <dd className="mt-0.5 text-[length:var(--text-lg)] leading-none text-fg">
              {chosen.charAt(0).toUpperCase()}
              {chosen.slice(1)}
            </dd>
          </div>
        ) : null}
      </dl>

      {scenarios.length >= 2 && recommended ? (
        <ScenarioComparison
          scenarios={scenarios}
          recommended={recommended as ScenarioName}
          reason={textAt(payload, "media_plan.recommendation_reason") ?? undefined}
        />
      ) : null}

      {monthly.length >= 2 && isRecord(band) ? (
        <ForecastLine
          forecast={{
            monthly_totals: monthly as never,
            confidence_band: band as never,
          }}
        />
      ) : null}

      {allocation.length ? (
        <ChartFrame
          title="Where the envelope goes"
          note="Campaign by market by funnel stage, as gate G3 approved it."
        >
          <div className="overflow-x-auto">
            <Table label="The approved budget allocation">
              <thead>
                <Tr>
                  <Th>Campaign</Th>
                  <Th>Market</Th>
                  <Th>Stage</Th>
                  <Th className="text-right">Share</Th>
                  <Th className="text-right">Monthly</Th>
                  <Th className="text-right">Forecast CPA</Th>
                  <Th className="text-right">Target CPA</Th>
                </Tr>
              </thead>
              <tbody>
                {allocation.map((row, index) => {
                  const money = typeof row.usd === "number" ? row.usd : null;
                  const share = capValue && money !== null ? money / capValue : null;
                  return (
                    <Tr key={`${str(row.campaign_ref) ?? index}-${str(row.market) ?? ""}-${index}`}>
                      <Td className="font-mono text-xs">{str(row.campaign_ref) ?? "—"}</Td>
                      <Td>{str(row.market) ?? "—"}</Td>
                      <Td className="text-fg-muted">{str(row.funnel_stage) ?? "—"}</Td>
                      <Td className="text-right">
                        <span className="inline-flex items-center gap-2">
                          {/* The bar is the comparison; the number beside it is
                              the figure. A track one step off the surface, so
                              the smallest share still reads as a measured
                              fraction rather than an empty cell. */}
                          <span
                            aria-hidden
                            className="hidden h-1 w-12 overflow-hidden rounded-full bg-border sm:inline-block"
                          >
                            <span
                              className="block h-full rounded-full bg-fg-subtle"
                              style={{ width: `${Math.max((share ?? 0) * 100, 2)}%` }}
                            />
                          </span>
                          <span data-numeric className="text-fg">
                            {share === null ? "—" : `${(share * 100).toFixed(1)}%`}
                          </span>
                        </span>
                      </Td>
                      <Td data-numeric className="text-right">
                        {money === null ? "—" : usd(money, 0)}
                      </Td>
                      <Td data-numeric className="text-right">
                        {typeof row.forecast_cpa_usd === "number"
                          ? usd(row.forecast_cpa_usd, 0)
                          : "—"}
                      </Td>
                      <Td data-numeric className="text-right text-fg-muted">
                        {typeof row.target_cpa_usd === "number" ? usd(row.target_cpa_usd, 0) : "—"}
                      </Td>
                    </Tr>
                  );
                })}
              </tbody>
            </Table>
          </div>
        </ChartFrame>
      ) : null}

      {Array.isArray(rules) && rules.length ? (
        <div className="border-t pt-3">
          <h4 className="text-sm font-medium text-fg">When money moves</h4>
          <ul className="mt-1.5 space-y-1 text-sm text-fg-muted">
            {rules.map((rule, index) => (
              <li key={index} className="flex gap-2">
                <span aria-hidden className="text-fg-subtle">
                  ·
                </span>
                <span>{typeof rule === "string" ? rule : JSON.stringify(rule)}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// channel slate
// ---------------------------------------------------------------------------

/**
 * Launch waves on a horizontal timeline, criteria on hover (§15.3 D).
 *
 * Position carries the wave and the label carries the channel, so colour is
 * left alone — five channel hues would be a categorical palette this design
 * system does not have, spent on a dimension the row already names in words.
 * The one place colour appears is the wave a campaign belongs to, as emphasis
 * on the first wave, because "what launches now" is the question asked here.
 */
export function ChannelSlateSection({ payload }: SectionProps) {
  const waves = rowsAt(payload, "channel_slate.waves");
  const entries = rowsAt(payload, "channel_slate.entries");

  if (entries.length === 0) {
    return (
      <SectionMissing
        what="No channel slate yet"
        why="Stage 2.3 chooses the campaign types and the launch order; gate G4 approves them."
      />
    );
  }

  const span = Math.max(
    ...waves.map((wave) => (typeof wave.ends_week === "number" ? wave.ends_week : 0)),
    1,
  );

  return (
    <div className="space-y-4">
      {waves.length ? (
        <ChartFrame
          title="Launch order"
          note={`${waves.length} waves across ${span} weeks. Hover a campaign for its entry and exit criteria.`}
        >
          <ol className="space-y-3">
            {waves.map((wave, index) => {
              const start = typeof wave.starts_week === "number" ? wave.starts_week : 1;
              const end = typeof wave.ends_week === "number" ? wave.ends_week : start;
              const refs = Array.isArray(wave.campaign_refs)
                ? wave.campaign_refs.filter((ref): ref is string => typeof ref === "string")
                : [];
              return (
                <li key={str(wave.label) ?? index}>
                  <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
                    <p className="text-sm font-medium text-fg">
                      {str(wave.label) ?? `Wave ${index + 1}`}
                    </p>
                    <p data-numeric className="text-xs text-fg-subtle">
                      weeks {start}–{end}
                    </p>
                  </div>
                  {/* The track is the quarter; the span is this wave in it. */}
                  <div
                    aria-hidden
                    className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-border"
                  >
                    <div
                      className={cn(
                        "h-full rounded-full",
                        index === 0 ? "bg-accent" : "bg-fg-subtle",
                      )}
                      style={{
                        marginLeft: `${((start - 1) / span) * 100}%`,
                        width: `${Math.max(((end - start + 1) / span) * 100, 2)}%`,
                      }}
                    />
                  </div>
                  <ul className="mt-1.5 flex flex-wrap gap-1.5">
                    {refs.map((ref) => {
                      const entry = entries.find((row) => row.campaign_ref === ref);
                      return (
                        <li key={ref}>
                          <Tooltip
                            content={
                              entry ? (
                                <span className="block max-w-72 space-y-1">
                                  <span className="block">
                                    <span className="text-fg-subtle">In: </span>
                                    {str(entry.entry_criteria) ?? "not stated"}
                                  </span>
                                  <span className="block">
                                    <span className="text-fg-subtle">Out: </span>
                                    {str(entry.exit_criteria) ?? "not stated"}
                                  </span>
                                </span>
                              ) : (
                                "This campaign is in the wave but carries no slate entry."
                              )
                            }
                          >
                            <span
                              tabIndex={0}
                              className="inline-flex items-baseline gap-1.5 rounded-full border bg-surface px-2 py-0.5 text-xs"
                            >
                              <span className="font-mono text-fg">{ref}</span>
                              {entry && str(entry.channel) ? (
                                <span className="text-fg-subtle">
                                  {(str(entry.channel) ?? "").replace(/_/g, " ")}
                                </span>
                              ) : null}
                            </span>
                          </Tooltip>
                        </li>
                      );
                    })}
                  </ul>
                  {str(wave.gate) ? (
                    <p className="mt-1 text-xs text-fg-subtle">{str(wave.gate)}</p>
                  ) : null}
                </li>
              );
            })}
          </ol>
        </ChartFrame>
      ) : null}

      <div className="overflow-x-auto">
        <Table label="The channel slate">
          <thead>
            <Tr>
              <Th>Campaign</Th>
              <Th>Channel</Th>
              <Th>Market</Th>
              <Th className="text-right">Wave</Th>
              <Th>Why</Th>
            </Tr>
          </thead>
          <tbody>
            {entries.map((row, index) => (
              <Tr key={`${str(row.campaign_ref) ?? index}-${str(row.channel) ?? ""}`}>
                <Td className="font-mono text-xs">{str(row.campaign_ref) ?? "—"}</Td>
                <Td>{(str(row.channel) ?? "—").replace(/_/g, " ")}</Td>
                <Td>{str(row.market) ?? "—"}</Td>
                <Td data-numeric className="text-right">
                  {typeof row.wave === "number" ? row.wave : "—"}
                </Td>
                <Td className="text-fg-muted">
                  {/* The bound goes on a block INSIDE the cell: `max-width` on
                      a `<td>` is ignored under `table-layout: auto`, so the
                      cell sizes to the sentence and `min-w-max` then sizes the
                      whole table to the cell. `Table`'s own header warns about
                      the prose column; it does not say that the obvious place
                      to put the fix is the one place CSS ignores it. */}
                  <span className="block max-w-[36ch]">{str(row.rationale) ?? "—"}</span>
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// measurement plan
// ---------------------------------------------------------------------------

/**
 * What settles an argument, and what has to be true before it can.
 *
 * The prerequisites are a checklist with an owner per row because §15.3 D asks
 * for one, and because a measurement plan whose prerequisites have no owner is
 * a plan that will not be built. Blocking ones are separated from the rest: an
 * unowned nice-to-have and an unowned blocker are not the same risk.
 */
export function MeasurementSection({ payload }: SectionProps) {
  const truth = textAt(payload, "measurement_plan.source_of_truth");
  const metrics = rowsAt(payload, "measurement_plan.metric_definitions");
  const prerequisites = rowsAt(payload, "measurement_plan.prerequisites");
  const offline = at(payload, "measurement_plan.offline_conversion_plan");
  const discrepancies = rowsAt(payload, "measurement_plan.known_discrepancies");

  if (!truth && metrics.length === 0) {
    return (
      <SectionMissing
        what="No measurement plan yet"
        why="Nodes 2.5.1 and 2.5.2 decide the source of truth and the offline conversion pipeline."
      />
    );
  }

  const tolerance = at(payload, "measurement_plan.tolerance_pct");
  const blocking = prerequisites.filter((row) => row.blocking === true);

  return (
    <div className="space-y-4">
      <div>
        <p className="text-sm text-fg">
          <span className="text-fg-muted">Source of truth: </span>
          <span className="font-medium">{(truth ?? "—").replace(/_/g, " ")}</span>
          {typeof tolerance === "number" ? (
            <span className="text-fg-muted">
              {" "}
              · others may differ by up to{" "}
              <span data-numeric>{tolerance.toFixed(0)}%</span>
            </span>
          ) : null}
        </p>
        {textAt(payload, "measurement_plan.rationale") ? (
          <p className="mt-1 text-sm text-fg-muted">
            {textAt(payload, "measurement_plan.rationale")}
          </p>
        ) : null}
      </div>

      {isRecord(offline) ? <OfflinePipeline offline={offline} /> : null}

      {metrics.length ? (
        <div className="overflow-x-auto">
          <Table label="Metric definitions">
            <thead>
              <Tr>
                <Th>Metric</Th>
                <Th>Definition</Th>
                <Th>System</Th>
                <Th>Owner</Th>
              </Tr>
            </thead>
            <tbody>
              {metrics.map((row, index) => (
                <Tr key={str(row.metric) ?? index}>
                  <Td className="font-mono text-xs">
                    {(str(row.metric) ?? "—").replace(/_/g, " ")}
                  </Td>
                  <Td className="text-fg-muted">
                    <span className="block max-w-[40ch]">{str(row.definition) ?? "—"}</span>
                  </Td>
                  <Td>{(str(row.system) ?? "—").replace(/_/g, " ")}</Td>
                  <Td>{str(row.owner) ?? "unassigned"}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </div>
      ) : null}

      {prerequisites.length ? (
        <div className="border-t pt-3">
          <h4 className="text-sm font-medium text-fg">
            Before any of this measures anything
            {blocking.length ? (
              <span className="ml-2 text-xs font-normal text-status-gate">
                <span data-numeric>{blocking.length}</span> blocking
              </span>
            ) : null}
          </h4>
          <ul className="mt-2 space-y-1.5">
            {prerequisites.map((row, index) => (
              <li key={str(row.what) ?? index} className="flex items-baseline gap-2 text-sm">
                <span
                  aria-hidden
                  className={cn(
                    "mt-1.5 size-1.5 shrink-0 rounded-full",
                    row.blocking === true ? "bg-status-gate" : "bg-fg-subtle",
                  )}
                />
                <span className="min-w-0 flex-1">
                  <span className="text-fg">{str(row.what) ?? "—"}</span>
                  <span className="text-fg-muted"> — {str(row.owner) ?? "unassigned"}</span>
                  {str(row.status) ? (
                    <span className="text-fg-subtle">
                      {" "}
                      · {(str(row.status) ?? "").replace(/_/g, " ")}
                    </span>
                  ) : null}
                </span>
                {row.blocking === true ? (
                  <span className="shrink-0 text-xs text-status-gate">blocking</span>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {discrepancies.length ? (
        <p className="border-t pt-3 text-xs text-fg-muted">
          Known to disagree:{" "}
          {discrepancies
            .map((row) => {
              const gap = typeof row.expected_pct === "number" ? `${row.expected_pct}%` : "";
              return `${str(row.between) ?? "two systems"}${gap ? ` by ~${gap}` : ""}`;
            })
            .join("; ")}
          .
        </p>
      ) : null}
    </div>
  );
}

/**
 * The offline conversion pipeline as a small flow (§15.3 D).
 *
 * Three steps and two arrows. A real flow diagram for a three-node linear
 * pipeline would be a canvas, a layout pass and a legend spent on something a
 * sentence already conveys — and this one has to survive 390px.
 */
function OfflinePipeline({ offline }: { offline: Record<string, unknown> }) {
  const capture = offline.gclid_capture;
  const upload = offline.upload;
  const consent = offline.consent;
  const stages = isRecord(upload) && Array.isArray(upload.stages) ? upload.stages.filter(isRecord) : [];

  return (
    <div className="rounded-[var(--radius)] border bg-surface p-3">
      <ol className="flex flex-col gap-2 sm:flex-row sm:items-stretch">
        <PipelineStep
          title="Capture"
          lines={
            isRecord(capture)
              ? [
                  str(capture.storage_object),
                  typeof capture.retention_days === "number"
                    ? `kept ${capture.retention_days} days`
                    : null,
                  str(capture.consent_basis)?.replace(/_/g, " ") ?? null,
                ]
              : []
          }
        />
        <PipelineArrow />
        <PipelineStep
          title="Upload"
          lines={
            isRecord(upload)
              ? [str(upload.mechanism), str(upload.frequency)]
              : []
          }
        />
        <PipelineArrow />
        <PipelineStep
          title="Match"
          lines={stages.map((stage) => {
            const from = str(stage.crm_stage)?.replace(/_/g, " ") ?? "—";
            return `${from} → ${str(stage.conversion_action) ?? "—"}`;
          })}
        />
      </ol>
      {isRecord(consent) && Array.isArray(consent.markets_blocked) && consent.markets_blocked.length ? (
        <p className="mt-2 text-xs text-status-gate">
          Not planned in {consent.markets_blocked.join(", ")} — the consent gate records no lawful
          basis for uploading customer data there.
        </p>
      ) : null}
    </div>
  );
}

function PipelineStep({ title, lines }: { title: string; lines: (string | null)[] }) {
  const shown = lines.filter((line): line is string => Boolean(line));
  return (
    <li className="min-w-0 flex-1 rounded-[6px] border bg-surface-raised px-2.5 py-2">
      <p className="text-xs font-medium text-fg">{title}</p>
      {shown.length ? (
        <ul className="mt-0.5 space-y-0.5">
          {shown.map((line) => (
            <li key={line} className="text-xs break-words text-fg-muted">
              {line}
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-0.5 text-xs text-fg-subtle">not planned</p>
      )}
    </li>
  );
}

function PipelineArrow() {
  return (
    <li
      aria-hidden
      className="flex shrink-0 items-center justify-center text-fg-subtle sm:px-0.5"
    >
      <ArrowRight className="size-3.5 rotate-90 sm:rotate-0" />
    </li>
  );
}

// ---------------------------------------------------------------------------
// test backlog
// ---------------------------------------------------------------------------

type SortKey = "ice" | "days" | "conv";

/**
 * The ranked backlog, default by ICE (§15.3 D).
 *
 * `required_conv_per_arm` and `est_days_to_significance` are columns rather
 * than details, because "this test needs 147 days" is the reason not to
 * schedule it and a test that cannot finish inside the quarter is called out on
 * its row. A backlog sorted only by ICE puts an unfinishable test at the top.
 */
export function BacklogSection({
  payload,
  calcs,
  projectId,
  sort,
  onSort,
}: SectionProps & { sort: SortKey; onSort: (key: SortKey) => void }) {
  const tests = rowsAt(payload, "experiment_backlog");

  if (tests.length === 0) {
    return (
      <SectionMissing
        what="No test backlog yet"
        why="Node 2.5.3 ranks the experiments and sizes each one for significance."
      />
    );
  }

  const sorted = [...tests].sort((left, right) => {
    if (sort === "ice") return iceOf(right) - iceOf(left);
    if (sort === "days") return daysOf(left) - daysOf(right);
    return convOf(left) - convOf(right);
  });

  return (
    <div className="overflow-x-auto">
      <Table label="The test backlog, ranked">
        <thead>
          <Tr>
            <Th>Test</Th>
            <Th className="text-right">
              <SortButton active={sort === "ice"} onClick={() => onSort("ice")}>
                ICE
              </SortButton>
            </Th>
            <Th className="text-right">
              <SortButton active={sort === "conv"} onClick={() => onSort("conv")}>
                Conv / arm
              </SortButton>
            </Th>
            <Th className="text-right">
              <SortButton active={sort === "days"} onClick={() => onSort("days")}>
                Days to call it
              </SortButton>
            </Th>
          </Tr>
        </thead>
        <tbody>
          {sorted.map((row, index) => {
            const days = daysOf(row);
            const fits = row.fits_in_quarter !== false && days <= 90;
            return (
              <Tr key={str(row.name) ?? index}>
                <Td>
                  <span className="block max-w-[44ch] text-fg">{str(row.name) ?? "—"}</span>
                  {str(row.hypothesis) ? (
                    <span className="mt-0.5 block max-w-[44ch] text-xs text-fg-muted">
                      {str(row.hypothesis)}
                    </span>
                  ) : null}
                  {!fits ? (
                    <span className="mt-0.5 block text-xs text-status-gate">
                      Cannot reach significance inside a quarter.
                    </span>
                  ) : null}
                </Td>
                <Td data-numeric className="text-right">
                  {iceOf(row).toFixed(1)}
                </Td>
                <Td className="text-right">
                  <Figure
                    value={figure(row.required_conv_per_arm)}
                    unit="count"
                    calcs={calcs}
                    projectId={projectId}
                    label={`Conversions per arm for ${str(row.name) ?? "this test"}`}
                  />
                </Td>
                <Td className="text-right">
                  <span className={cn("inline-flex", !fits && "text-status-gate")}>
                    <Figure
                      value={figure(row.est_days_to_significance)}
                      unit="days"
                      calcs={calcs}
                      projectId={projectId}
                      label={`Days to significance for ${str(row.name) ?? "this test"}`}
                    />
                  </span>
                </Td>
              </Tr>
            );
          })}
        </tbody>
      </Table>
    </div>
  );
}

function SortButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn("hover:text-fg", active ? "text-fg underline decoration-dotted" : "")}
    >
      {children}
    </button>
  );
}

function iceOf(row: Record<string, unknown>): number {
  const ice = row.ice;
  if (isRecord(ice) && typeof ice.score === "number") return ice.score;
  return 0;
}

/**
 * A figure's scalar for sorting, whether it arrived traced or plain.
 *
 * Unsortable rows sink rather than float: a test whose sizing is missing is not
 * the fastest test in the backlog, and putting it at the top is the one ranking
 * error that would get it scheduled.
 */
function scalarOf(value: unknown): number {
  return figureValue(figure(value)) ?? Number.MAX_SAFE_INTEGER;
}

function daysOf(row: Record<string, unknown>): number {
  return scalarOf(row.est_days_to_significance);
}

function convOf(row: Record<string, unknown>): number {
  return scalarOf(row.required_conv_per_arm);
}

export type { SortKey };
