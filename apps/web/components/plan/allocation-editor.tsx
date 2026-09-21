"use client";

import { useMutation } from "@tanstack/react-query";
import { Calculator, TriangleAlert } from "lucide-react";
import { Fragment, useState } from "react";

import { ForecastLine, ScenarioSelector } from "@/components/plan/forecast-chart";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import {
  recalcApproval,
  type ApprovalItem,
  type Recalc,
  type RecalcRow,
} from "@/lib/api/approvals";
import type {
  AllocationLine,
  BudgetAllocationProposal,
  BudgetScenariosOutput,
  DemandForecastOutput,
  ScenarioName,
} from "@/lib/api/plan";
import { compactNumber, usd } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Gate G3, the one gate that hands a number back to the engine
 * (Stage 02 PRD §15.3 C).
 *
 * Everything numeric here comes from the server. The editor holds the
 * approver's typed figures and nothing else: what an edit *buys* is
 * `POST /approvals/{id}/recalc`, what counts as under-funded is that response's
 * `below_floor`, and what the split adds up to is the one subtraction the
 * remainder chip needs. §15.4 rule 2 calls a duplicated forecast in TypeScript
 * a bug rather than an optimisation, and the way to keep that rule is to have
 * nowhere in this file that could hold one.
 *
 * **The envelope is chosen, never typed.** The three scenarios carry their own
 * monthly total, quarterly total and allocated total, all computed by 2.2.3; a
 * free-text envelope would leave the plan's quarterly figure to be invented
 * here. An approver who wants a number no scenario offers is rejecting the
 * gate, which sends the question back to the engine that can answer it.
 */

/** `allocation._unit_key` on the server: the three identity columns, "/"-joined. */
export function unitKey(line: {
  campaign_ref: string;
  market: string;
  funnel_stage: string;
}): string {
  return [line.campaign_ref || "-", line.market || "-", line.funnel_stage || "-"].join("/");
}

/**
 * What the approver has typed, and the server's answer to it.
 *
 * `recalcFor` is the signature of the edit `recalc` answers. Typing after a
 * recalculation makes the answer stale, and a stale answer on screen beside a
 * changed number is worse than no answer: it reads as if the edit has been
 * checked.
 */
export type BudgetEdit = {
  scenario: ScenarioName;
  /** The allocation these figures started from — the chosen scenario's, or the proposal's. */
  source: AllocationLine[];
  envelopeUsd: number;
  /** Unit key → monthly dollars. Every line in `source` has an entry. */
  lines: Record<string, number>;
  recalc: Recalc | null;
  recalcFor: string | null;
};

/**
 * The figures currently on screen, as `source` orders them.
 *
 * Every total, every signature and the recalc request itself read this, so a
 * what-if restored from a gate that was edited under a different scenario
 * cannot leave an amount in the state that no row displays and no request
 * sends — but that the remainder chip would still count.
 */
function amounts(edit: Pick<BudgetEdit, "source" | "lines">): [string, number][] {
  return edit.source.map((line) => {
    const key = unitKey(line);
    return [key, edit.lines[key] ?? line.usd];
  });
}

function signature(edit: Pick<BudgetEdit, "source" | "envelopeUsd" | "lines">): string {
  const lines = amounts(edit)
    .map(([key, value]) => `${key}=${value.toFixed(2)}`)
    .sort();
  return `${edit.envelopeUsd.toFixed(2)}|${lines.join(",")}`;
}

/** The state a freshly opened card starts in. */
export function initialEdit(approval: ApprovalItem, proposal: BudgetAllocationProposal): BudgetEdit {
  const source = proposal.allocation;
  const stored = approval.recalc_state;

  // Somebody already ran a what-if against this gate. Restore their figures
  // rather than the draft's: the point of persisting `recalc_state` is that a
  // second approver opens the card on the working that has already been done.
  if (stored && stored.allocation.length > 0) {
    const lines: Record<string, number> = {};
    for (const line of source) lines[unitKey(line)] = line.usd;
    const known = new Set(Object.keys(lines));
    for (const row of stored.allocation) {
      // A what-if run against another scenario's line-up carries units this
      // proposal does not. Ignoring them keeps the restored total equal to what
      // the rows on screen add up to.
      const key = unitKey(row);
      if (known.has(key)) lines[key] = row.requested_usd;
    }
    const restored = { source, envelopeUsd: stored.envelope_usd, lines };
    return {
      scenario: proposal.chosen_scenario,
      ...restored,
      recalc: stored,
      recalcFor: signature(restored),
    };
  }

  const lines: Record<string, number> = {};
  for (const line of source) lines[unitKey(line)] = line.usd;
  return {
    scenario: proposal.chosen_scenario,
    source,
    envelopeUsd: proposal.envelope.monthly_cap_usd,
    lines,
    recalc: null,
    recalcFor: null,
  };
}

/** Half a cent — `allocation.CENT`, the residual the server treats as exact. */
const CENT = 0.005;

export type EditState = {
  allocatedUsd: number;
  /** Envelope minus what is allocated. Positive means money with nowhere to go. */
  remainderUsd: number;
  balanced: boolean;
  dirty: boolean;
  /** A recalculation that answers the figures currently on screen. */
  answered: Recalc | null;
  /** Why Approve is refused, in a whole sentence, or null when it is not. */
  blocked: string | null;
};

export function readEdit(proposal: BudgetAllocationProposal, edit: BudgetEdit): EditState {
  const allocatedUsd = amounts(edit).reduce((total, [, value]) => total + value, 0);
  const remainderUsd = edit.envelopeUsd - allocatedUsd;
  // "Non-zero" as a currency chip shows it. Chasing a sub-cent residual across
  // 160 rows is not a decision anybody is trying to make, and the server treats
  // the same residual as exact.
  const balanced = Math.abs(remainderUsd) < 0.01;

  // Values, never array identity: the inbox re-fetches on a timer and hands
  // this card a structurally identical `allocation` array every poll, so a
  // reference comparison would report an edit nobody made. The scenario is part
  // of it because two scenarios can price out identically — the learning-period
  // floor can raise cautious, expected and aggressive to the same envelope —
  // and choosing a different one is still a decision the plan has to record.
  const dirty =
    edit.scenario !== proposal.chosen_scenario ||
    edit.envelopeUsd !== proposal.envelope.monthly_cap_usd ||
    edit.source.length !== proposal.allocation.length ||
    proposal.allocation.some((line) => edit.lines[unitKey(line)] !== line.usd);

  const current = signature(edit);
  const answered = edit.recalc !== null && edit.recalcFor === current ? edit.recalc : null;

  let blocked: string | null = null;
  if (!balanced) {
    blocked =
      remainderUsd > 0
        ? `${usd(remainderUsd)} of the envelope is unallocated.`
        : `The split commits ${usd(-remainderUsd)} more than the envelope.`;
  } else if (dirty && answered === null) {
    blocked = "Recalculate to see what this edit buys before approving it.";
  }

  return { allocatedUsd, remainderUsd, balanced, dirty, answered, blocked };
}

/**
 * The edited proposal, built from the server's own re-forecast.
 *
 * Only called with a recalculation that answers the figures on screen, so every
 * number written here was computed by `allocation.whatif_v1`. The fields the
 * what-if does not return — the target CPA, the efficiency, the absorption cap —
 * are carried over from the line it re-forecast.
 *
 * `usd` is what the approver **allocated**, not what the forecast thinks can be
 * absorbed. The envelope check on the way in sums this field, and a plan whose
 * rows do not add up to its own total is a plan that gets queried instead of
 * approved. Where the two differ, `cap_applied` and the card say so.
 */
export function buildEditedProposal(
  proposal: BudgetAllocationProposal,
  edit: BudgetEdit,
  recalc: Recalc,
  scenarios: BudgetScenariosOutput | null,
): Record<string, unknown> {
  const rows = new Map<string, RecalcRow>();
  for (const row of recalc.allocation) rows.set(unitKey(row), row);

  const allocation = edit.source.map((line) => {
    const row = rows.get(unitKey(line));
    if (!row) return line;
    return {
      ...line,
      usd: row.requested_usd,
      pct: row.pct,
      est_conv: row.est_conv,
      est_clicks: row.est_clicks,
      below_floor: row.below_floor,
      cap_applied: row.cap_applied,
    };
  });

  const chosen = scenarios?.scenarios.find((scenario) => scenario.name === edit.scenario);
  const envelope =
    chosen && edit.scenario !== proposal.chosen_scenario
      ? {
          ...proposal.envelope,
          monthly_cap_usd: recalc.envelope_usd,
          quarterly_cap_usd: chosen.quarterly_total_usd,
          scenario_total_usd: chosen.monthly_total_usd,
          unallocated_usd: chosen.unallocated_usd,
        }
      : { ...proposal.envelope, monthly_cap_usd: recalc.envelope_usd };

  const edits = edit.source
    .filter((line) => {
      const row = rows.get(unitKey(line));
      return row !== undefined && Math.abs(row.requested_usd - line.usd) >= CENT;
    })
    .map((line) => ({
      campaign_ref: line.campaign_ref,
      market: line.market,
      funnel_stage: line.funnel_stage,
      from_usd: line.usd,
      to_usd: rows.get(unitKey(line))?.requested_usd,
    }));

  return {
    ...proposal,
    chosen_scenario: edit.scenario,
    envelope,
    allocation,
    edits_applied: edits,
    // The what-if's row goes first: it is the calculation that produced these
    // figures, and the draft's ids only justify the ones the edit left alone.
    calc_evidence_ids: [
      recalc.calc_evidence_id,
      ...((proposal as unknown as { calc_evidence_ids?: string[] }).calc_evidence_ids ?? []),
    ],
  };
}

// ---------------------------------------------------------------------------
// The editor
// ---------------------------------------------------------------------------

export function AllocationEditor({
  approval,
  proposal,
  scenarios,
  forecast,
  value,
  onChange,
  readOnly,
}: {
  approval: ApprovalItem;
  proposal: BudgetAllocationProposal;
  /** 2.2.3's output, when the console could read it. Without it there is no selector. */
  scenarios: BudgetScenariosOutput | null;
  /** 2.2.1's output, for the months behind the envelope. Optional context. */
  forecast: DemandForecastOutput | null;
  value: BudgetEdit;
  onChange: (next: BudgetEdit) => void;
  readOnly: boolean;
}) {
  const state = readEdit(proposal, value);

  const recalc = useMutation({
    mutationFn: () =>
      recalcApproval(approval.id, {
        allocation: value.source.map((line) => ({
          campaign_ref: line.campaign_ref,
          market: line.market,
          funnel_stage: line.funnel_stage,
          usd: value.lines[unitKey(line)] ?? line.usd,
        })),
        envelope_usd: value.envelopeUsd,
      }),
    onSuccess: (result) => {
      onChange({ ...value, recalc: result, recalcFor: signature(value) });
    },
    onError: (error) => {
      toast.error("That split could not be re-forecast", {
        description:
          error instanceof ApiError ? error.detail : "The server did not answer. Try again.",
      });
    },
  });

  function setLine(key: string, amount: number) {
    onChange({ ...value, lines: { ...value.lines, [key]: amount } });
  }

  function chooseScenario(name: ScenarioName) {
    const scenario = scenarios?.scenarios.find((item) => item.name === name);
    if (!scenario) return;
    const lines: Record<string, number> = {};
    for (const line of scenario.allocation) lines[unitKey(line)] = line.usd;
    onChange({
      ...value,
      scenario: name,
      source: scenario.allocation,
      // The allocated total, not the scenario headline: a measured absorption
      // cap can leave part of a scenario unspendable, and §12 invariant 4 is
      // about what the lines add up to.
      envelopeUsd: scenario.allocated_usd,
      lines,
    });
  }

  /** Hand the residual to the largest line, so a rounding gap is one click. */
  function absorbRemainder() {
    const biggest = [...value.source].sort(
      (a, b) => (value.lines[unitKey(b)] ?? 0) - (value.lines[unitKey(a)] ?? 0),
    )[0];
    if (!biggest) return;
    const key = unitKey(biggest);
    const next = Math.max(0, (value.lines[key] ?? 0) + state.remainderUsd);
    setLine(key, Math.round(next * 100) / 100);
  }

  const answered = state.answered;
  const byUnit = new Map<string, RecalcRow>();
  if (answered) for (const row of answered.allocation) byUnit.set(unitKey(row), row);
  const floors = new Map<string, number>();
  if (answered) for (const row of answered.below_floor) floors.set(row.unit, row.floor_usd);

  return (
    <div className="space-y-4">
      {scenarios && scenarios.scenarios.length > 1 ? (
        <ScenarioSelector
          scenarios={scenarios.scenarios}
          selected={value.scenario}
          recommended={scenarios.recommended}
          onSelect={chooseScenario}
          disabled={readOnly}
        />
      ) : null}

      <Rationale proposal={proposal} />

      <div className="flex flex-wrap items-center justify-between gap-2">
        <Remainder state={state} envelopeUsd={value.envelopeUsd} />
        {!readOnly && !state.balanced ? (
          <Button variant="ghost" size="sm" onClick={absorbRemainder}>
            Give it to the largest line
          </Button>
        ) : null}
      </div>

      <AllocationTable
        lines={value.source}
        amounts={value.lines}
        envelopeUsd={value.envelopeUsd}
        recalcRows={byUnit}
        floors={floors}
        readOnly={readOnly}
        onEdit={setLine}
      />

      {!readOnly ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="secondary"
            onClick={() => recalc.mutate()}
            disabled={recalc.isPending || !state.balanced}
            title={state.balanced ? undefined : "The split has to add up to the envelope first"}
          >
            {recalc.isPending ? <Spinner label="Recalculating" /> : <Calculator aria-hidden />}
            Recalculate
          </Button>
          <p className="text-xs text-fg-subtle">
            No model call and no run advance — try as many splits as you like.
          </p>
        </div>
      ) : null}

      {answered ? <WhatItBuys recalc={answered} proposal={proposal} /> : null}

      <LearningWarnings proposal={proposal} />

      {forecast ? (
        <details className="group/demand rounded-[var(--radius)] border bg-surface">
          <summary className="cursor-pointer list-none px-3.5 py-2.5 text-xs text-fg-muted hover:text-fg [&::-webkit-details-marker]:hidden">
            <span className="group-open/demand:hidden">
              Show the demand this envelope is priced against
            </span>
            <span className="hidden group-open/demand:inline">Hide the demand forecast</span>
          </summary>
          <div className="px-3.5 pb-3.5">
            <ForecastLine forecast={forecast} />
          </div>
        </details>
      ) : null}
    </div>
  );
}

function Rationale({ proposal }: { proposal: BudgetAllocationProposal }) {
  return (
    <div className="rounded-[var(--radius)] border bg-surface px-3.5 py-3 text-sm">
      <p className="text-fg-muted">{proposal.rationale}</p>
      <p className="mt-1.5 text-fg-subtle">
        <span className="font-medium text-fg-muted">What would change it: </span>
        {proposal.what_would_change_it}
      </p>
    </div>
  );
}

/**
 * The remainder chip (PRD §15.3 C.3).
 *
 * Names the direction as well as the amount: "$1,400 unallocated" and "$1,400
 * over" are opposite problems with opposite fixes, and a chip that shows only a
 * magnitude makes the approver work out which one they have.
 */
function Remainder({ state, envelopeUsd }: { state: EditState; envelopeUsd: number }) {
  return (
    <p className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-sm">
      <span className="text-fg-muted">Allocated</span>
      <span data-numeric className="font-medium text-fg">
        {usd(state.allocatedUsd)}
      </span>
      <span className="text-fg-subtle">of</span>
      <span data-numeric className="text-fg">
        {usd(envelopeUsd)}
      </span>
      {state.balanced ? (
        <Badge tone="neutral">Balanced</Badge>
      ) : (
        <Badge tone="warning">
          <TriangleAlert aria-hidden className="size-3 text-status-gate" />
          <span data-numeric>{usd(Math.abs(state.remainderUsd))}</span>
          {/* Its own element, not a bare sibling of the figure: JSX drops the
              newline between two expressions, so `{usd(x)}` followed by a
              string literal renders as "$1,400.00over". The badge's `gap-1` is
              what puts the space there. */}
          <span>{state.remainderUsd > 0 ? "unallocated" : "over"}</span>
        </Badge>
      )}
    </p>
  );
}

/**
 * The split, editable in either unit (PRD §15.3 C.2).
 *
 * Share and money are one figure shown twice — the share is of the envelope,
 * which is exactly how the server computes `pct`, so the two columns cannot
 * disagree with the re-forecast that follows.
 *
 * **Five columns, not seven.** Campaign, market and funnel stage are one thing
 * — the server joins them with slashes and calls it a unit — and as three
 * columns they pushed the identity of a row off the left edge of the console's
 * right-hand panel the moment anyone focused an input and the table scrolled to
 * it. A row whose campaign you cannot see is a row you cannot decide about.
 */
function AllocationTable({
  lines,
  amounts,
  envelopeUsd,
  recalcRows,
  floors,
  readOnly,
  onEdit,
}: {
  lines: AllocationLine[];
  amounts: Record<string, number>;
  envelopeUsd: number;
  recalcRows: Map<string, RecalcRow>;
  floors: Map<string, number>;
  readOnly: boolean;
  onEdit: (key: string, amount: number) => void;
}) {
  return (
    <div className="max-h-[28rem] overflow-y-auto rounded-[var(--radius)] border">
      <Table label="The monthly split, by campaign, market and funnel stage">
        <thead className="sticky top-0 z-10 bg-surface-raised">
          <Tr>
            <Th className="px-2.5">Unit</Th>
            <Th className="px-2.5 text-right">Share</Th>
            <Th className="px-2.5 text-right">Monthly</Th>
            <Th className="px-2.5 text-right">CPA</Th>
            <Th className="px-2.5 text-right">Conv</Th>
          </Tr>
        </thead>
        <tbody>
          {lines.map((line) => {
            const key = unitKey(line);
            const amount = amounts[key] ?? line.usd;
            const row = recalcRows.get(key);
            const floor = floors.get(key);
            const capped = row?.cap_applied ? row.wasted_usd : 0;
            const flagged = floor !== undefined || capped > 0.01;
            return (
              <Fragment key={key}>
                <Tr className={cn(flagged && "bg-status-gate/5")}>
                  <Td className={cn("max-w-56 px-2.5", flagged && "border-b-0 pb-1")}>
                    <span className="block truncate font-medium" title={line.campaign_ref}>
                      {line.campaign_ref}
                    </span>
                    <span className="block truncate text-xs text-fg-muted">
                      {line.market} · {line.funnel_stage}
                    </span>
                  </Td>
                  <Td className={cn("px-2.5 text-right", flagged && "border-b-0 pb-1")}>
                    <DraftInput
                      label={`Share of the envelope for ${key}`}
                      value={envelopeUsd > 0 ? (amount / envelopeUsd) * 100 : 0}
                      format={(value) => value.toFixed(1)}
                      onCommit={(next) => onEdit(key, (next / 100) * envelopeUsd)}
                      readOnly={readOnly || envelopeUsd <= 0}
                      suffix="%"
                    />
                  </Td>
                  <Td className={cn("px-2.5 text-right", flagged && "border-b-0 pb-1")}>
                    <DraftInput
                      label={`Monthly budget for ${key}`}
                      value={amount}
                      format={(value) => value.toFixed(0)}
                      onCommit={(next) => onEdit(key, next)}
                      readOnly={readOnly}
                      prefix="$"
                    />
                  </Td>
                  <Td
                    data-numeric
                    className={cn("px-2.5 text-right text-fg-muted", flagged && "border-b-0 pb-1")}
                  >
                    {usd(line.forecast_cpa_usd, 0)}
                  </Td>
                  <Td data-numeric className={cn("px-2.5 text-right", flagged && "border-b-0 pb-1")}>
                    {(row?.est_conv ?? line.est_conv).toFixed(1)}
                  </Td>
                </Tr>
                {/* Its own full-width row rather than a note in the first cell:
                    this table scrolls sideways in the console's right-hand
                    panel, and a warning parked in a column can be scrolled away
                    from the row it is about. Spanning every column, it cannot
                    be, and it has room for the whole sentence. */}
                {flagged ? (
                  <Tr className="bg-status-gate/5">
                    <Td colSpan={5} className="px-2.5 pt-0">
                      <span className="flex max-w-[21rem] items-start gap-1.5 text-xs whitespace-normal text-fg-muted">
                        <TriangleAlert
                          aria-hidden
                          className="mt-0.5 size-3 shrink-0 text-status-gate"
                        />
                        <span>
                          {floor !== undefined ? (
                            <>
                              {usd(floor - amount)} under the {usd(floor)} learning-period floor —
                              it would spend the month learning and never leave it.
                            </>
                          ) : (
                            <>
                              {usd(capped)} of this cannot be absorbed at the forecast impression
                              share.
                            </>
                          )}
                        </span>
                      </span>
                    </Td>
                  </Tr>
                ) : null}
              </Fragment>
            );
          })}
        </tbody>
      </Table>
    </div>
  );
}

/**
 * A number field that lets someone type.
 *
 * While the field has focus the typed text is what is shown, so a half-written
 * "1" on the way to "1500" is not reformatted under the cursor; every valid
 * keystroke still commits, so the remainder chip moves as the number does. On
 * blur the field goes back to showing the model's value, which is also how an
 * emptied field restores itself rather than silently meaning zero.
 */
function DraftInput({
  label,
  value,
  format,
  onCommit,
  readOnly,
  prefix,
  suffix,
}: {
  label: string;
  value: number;
  format: (value: number) => string;
  onCommit: (next: number) => void;
  readOnly: boolean;
  prefix?: string;
  suffix?: string;
}) {
  const [draft, setDraft] = useState<string | null>(null);

  if (readOnly) {
    return (
      <span data-numeric className="text-fg">
        {prefix}
        {format(value)}
        {suffix}
      </span>
    );
  }

  return (
    <span className="inline-flex items-baseline justify-end gap-0.5">
      {prefix ? <span className="text-fg-subtle">{prefix}</span> : null}
      <input
        type="text"
        inputMode="decimal"
        aria-label={label}
        value={draft ?? format(value)}
        onFocus={() => setDraft(format(value))}
        onBlur={() => setDraft(null)}
        onChange={(event) => {
          setDraft(event.target.value);
          const parsed = Number(event.target.value.replace(/[,\s]/g, ""));
          if (event.target.value.trim() !== "" && Number.isFinite(parsed) && parsed >= 0) {
            onCommit(parsed);
          }
        }}
        data-numeric
        className={cn(
          "rounded-[6px] border bg-surface px-1.5 py-1 text-right text-sm text-fg focus-visible:border-accent",
          suffix === "%" ? "w-14" : "w-20",
        )}
      />
      {suffix ? <span className="text-fg-subtle">{suffix}</span> : null}
    </span>
  );
}

/**
 * What the edit buys (PRD §15.3 C.4).
 *
 * Clicks, conversions and CPA are the server's. **Pipeline is not**: the PRD
 * asks for it here, `allocation.whatif_v1` does not return one, and the obvious
 * fix — conversions times the deal size — is precisely the duplicated formula
 * §15.4 rule 2 forbids. The draft's pipeline figure is shown for what it is
 * instead, and the gap is on the open-questions list rather than papered over.
 */
function WhatItBuys({
  recalc,
  proposal,
}: {
  recalc: Recalc;
  proposal: BudgetAllocationProposal;
}) {
  const withClicks = recalc.allocation.filter((row) => row.est_clicks !== null);
  const clicks = withClicks.reduce((total, row) => total + (row.est_clicks ?? 0), 0);
  const missing = recalc.allocation.length - withClicks.length;

  return (
    <section
      aria-label="What this split is forecast to buy"
      className="rounded-[var(--radius)] border border-accent/40 bg-accent-soft px-3.5 py-3"
    >
      <h4 className="text-xs font-medium tracking-wide text-fg-muted uppercase">
        What this split buys
      </h4>
      <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-4">
        <Figure
          label="Clicks"
          value={missing === recalc.allocation.length ? "—" : compactNumber(clicks)}
          note={
            missing > 0 && missing < recalc.allocation.length
              ? `${missing} line${missing === 1 ? "" : "s"} carry no click price`
              : undefined
          }
        />
        <Figure label="Conversions" value={recalc.est_conv.toFixed(1)} />
        <Figure
          label="CPA"
          value={recalc.est_cpa_usd === null ? "—" : usd(recalc.est_cpa_usd, 0)}
        />
        <Figure
          label="Spendable"
          value={usd(recalc.effective_usd, 0)}
          note={recalc.wasted_usd > 0.01 ? `${usd(recalc.wasted_usd, 0)} cannot be absorbed` : undefined}
        />
      </dl>
      <p className="mt-2 text-xs text-fg-subtle">
        {recalc.summary}
        {proposal.envelope.unallocated_reason ? ` ${proposal.envelope.unallocated_reason}` : ""}
      </p>
      {recalc.switched_off.length > 0 ? (
        <p className="mt-1.5 text-xs text-fg-muted">
          Switched off: {recalc.switched_off.join(", ")}.
        </p>
      ) : null}
    </section>
  );
}

function Figure({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-fg-muted">{label}</dt>
      <dd data-numeric className="text-[length:var(--text-md)] font-medium text-fg">
        {value}
      </dd>
      {note ? <p className="text-[0.6875rem] leading-tight text-fg-subtle">{note}</p> : null}
    </div>
  );
}

/**
 * Campaigns this envelope cannot get out of the learning period (2.2.2).
 *
 * A different question from the budget floor on a row: that one is "this line
 * is funded under the minimum", this one is "even funded, the conversions do
 * not arrive fast enough". Both are amber and both name their threshold, so
 * they are stated as two findings rather than merged into one warning that is
 * true for two different reasons.
 */
function LearningWarnings({ proposal }: { proposal: BudgetAllocationProposal }) {
  if (proposal.learning_warnings.length === 0) return null;
  return (
    <Alert tone="warning" title="Campaigns that may not leave the learning period">
      <ul className="space-y-1.5">
        {proposal.learning_warnings.map((warning) => (
          <li key={warning.campaign_ref}>
            <span className="font-medium text-fg">{warning.campaign_ref}</span> — forecast{" "}
            <span data-numeric>{warning.forecast_conv_30d.toFixed(1)}</span> conversions in 30 days
            against a threshold of <span data-numeric>{warning.threshold.toFixed(0)}</span>
            {warning.remedy ? `. ${warning.remedy.replace(/_/g, " ")}` : "."}
          </li>
        ))}
      </ul>
    </Alert>
  );
}
