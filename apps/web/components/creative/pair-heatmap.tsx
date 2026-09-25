"use client";

import { ArrowRight, CircleDashed, History, TriangleAlert, X, type LucideIcon } from "lucide-react";
import { useMemo, useState, type KeyboardEvent } from "react";

import { Table, Td, Th, Tr } from "@/components/ui/table";
import type { CreativeAssetItem, PairFlag, PairLabel } from "@/lib/api/creative-runs";
import { pairKey, pairState, position, type PairState, type StudioAd } from "@/lib/creative/ad-studio";
import { measuredText } from "@/lib/creative/char-count";
import { cn } from "@/lib/utils";

/**
 * A cell's state, worst first. Status, not category (§15.2 rule 14): each
 * state has its own shape — filled, outlined, dashed — and its own icon, so
 * none is read by colour alone, and the legend names every one.
 */
type CellState = "flagged" | "bad" | "order" | "ok" | "stale" | "unchecked";

const STATE: Record<CellState, { label: string; icon: LucideIcon | null; cell: string; ink: string }> = {
  flagged: {
    label: "Flagged",
    icon: X,
    cell: "border-status-failed-ink bg-status-failed-ink",
    ink: "text-bg",
  },
  bad: {
    label: "Redundant or contradictory",
    icon: TriangleAlert,
    cell: "border-2 border-status-gate-ink bg-surface-raised",
    ink: "text-status-gate-ink",
  },
  order: { label: "Reads in one order only", icon: ArrowRight, cell: "border-accent bg-accent-soft", ink: "text-accent-soft-fg" },
  ok: { label: "Reads well", icon: null, cell: "border-border bg-surface-hover", ink: "text-fg-muted" },
  stale: {
    label: "Checked before an edit",
    icon: History,
    cell: "border-dashed border-fg-subtle bg-surface-raised",
    ink: "text-fg-muted",
  },
  unchecked: {
    label: "Not checked — swapped in",
    icon: CircleDashed,
    cell: "border-dashed border-fg-subtle bg-surface-raised",
    ink: "text-fg-muted",
  },
};
const ORDER: CellState[] = ["flagged", "bad", "order", "stale", "unchecked", "ok"];

const FLAG: Record<PairFlag, string> = {
  duplicate: "duplicate",
  near_duplicate: "near duplicate",
  offer_conflict: "offer conflict",
  cta_collision: "call-to-action collision",
  claim_conflict: "claim conflict",
  keyword_stuffing: "keyword stuffing",
};
const LABEL: Record<PairLabel, string> = {
  reads_well: "reads well",
  redundant: "redundant",
  contradictory: "contradictory",
  order_dependent: "reads in one order only",
};

function stateOf(found: PairState): CellState {
  if (found.state === "unchecked") return "unchecked";
  if (found.state === "stale") return "stale";
  const { pair } = found;
  if (pair.flags.length > 0) return "flagged";
  if (pair.label === "redundant" || pair.label === "contradictory") return "bad";
  if (pair.label === "order_dependent") return "order";
  return "ok";
}

/** What 4.2.3 said, in words: the flags in its order, then its label. */
function findings(found: PairState): string {
  if (found.state === "unchecked") return "never paired by 4.2.3 — one of the two was swapped in";
  const said = [...found.pair.flags.map((flag) => FLAG[flag]), LABEL[found.pair.label]].join(", ");
  return found.state === "stale" ? `checked before an edit: ${said}` : said;
}

type Cell = { key: string; a: CreativeAssetItem; b: CreativeAssetItem; found: PairState; state: CellState };

/**
 * `PairHeatmap` — every headline pair and every headline–description pair the
 * ad can serve (Stage 04 PRD §15.4 E), one cell each, shaded by the worst
 * thing 4.2.3 found. Clicking a cell, or Enter on it, loads that pair into the
 * SERP preview. The grid is one tab stop; the arrows move within it (§15.2
 * rule 13). The readout names the focused or hovered pair in words, and the
 * table beneath lists every pair that is not simply reading well.
 */
export function PairHeatmap({
  ad,
  selected,
  onSelect,
}: {
  ad: StudioAd;
  selected: [string, string] | null;
  onSelect: (a: string, b: string) => void;
}) {
  const { hh, hd } = useMemo(() => {
    const cell = (a: CreativeAssetItem, b: CreativeAssetItem): Cell => {
      const found = pairState(ad, a, b);
      return { key: pairKey(a.id, b.id), a, b, found, state: stateOf(found) };
    };
    return {
      hh: ad.headlines.slice(1).map((row, i) => ad.headlines.slice(0, i + 1).map((col) => cell(row, col))),
      hd: ad.headlines.map((row) => ad.descriptions.map((col) => cell(row, col))),
    };
  }, [ad]);
  const all = [...hh.flat(), ...hd.flat()];
  const [readout, setReadout] = useState<Cell | null>(null);
  const counts = ORDER.map((state) => ({ state, count: all.filter((c) => c.state === state).length }));
  const attention = all
    .filter((c) => c.state !== "ok")
    .sort((x, y) => ORDER.indexOf(x.state) - ORDER.indexOf(y.state));
  const chosen = selected ? pairKey(selected[0], selected[1]) : null;

  return (
    <section aria-labelledby="pair-heatmap-title" className="flex min-w-0 flex-col gap-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 id="pair-heatmap-title" className="text-sm font-medium text-fg">
          Pairs
        </h2>
        <p className="text-xs tabular-nums text-fg-muted">
          {all.length} pairs · {hh.flat().length} headline × headline · {hd.flat().length} headline × description
        </p>
      </div>

      <ul aria-label="Pair states" className="flex flex-wrap gap-x-4 gap-y-1.5 text-xs">
        {counts
          .filter(({ state, count }) => count > 0 || state === "flagged" || state === "ok")
          .map(({ state, count }) => (
            <li key={state} className="inline-flex items-center gap-1.5 text-fg-muted">
              <Swatch state={state} />
              {STATE[state].label}
              <span className="tabular-nums text-fg">{count}</span>
            </li>
          ))}
      </ul>

      <div className="flex flex-col gap-6 rounded-token border bg-surface-raised p-4 xl:flex-row xl:items-start">
        <Grid
          title="Headline × headline"
          rows={hh}
          rowLabels={ad.headlines.slice(1).map((a) => position(ad, a))}
          colLabels={ad.headlines.slice(0, -1).map((a) => position(ad, a))}
          chosen={chosen}
          ad={ad}
          onSelect={onSelect}
          onReadout={setReadout}
        />
        <Grid
          title="Headline × description"
          rows={hd}
          rowLabels={ad.headlines.map((a) => position(ad, a))}
          colLabels={ad.descriptions.map((a) => position(ad, a))}
          chosen={chosen}
          ad={ad}
          onSelect={onSelect}
          onReadout={setReadout}
        />
      </div>

      <p aria-live="polite" className="min-h-10 text-xs text-fg-muted" data-testid="pair-readout">
        {readout ? (
          <>
            <span className="font-medium text-fg">
              {position(ad, readout.a)} with {position(ad, readout.b)}
            </span>{" "}
            — {findings(readout.found)}. “{measuredText(readout.a.surface, readout.a.text ?? "")}” · “
            {measuredText(readout.b.surface, readout.b.text ?? "")}”
          </>
        ) : (
          "Point at or move to a cell to read the pair; select it to load the pair into the preview."
        )}
      </p>

      {attention.length === 0 ? (
        <p className="text-sm text-fg-muted">Every pair reads well: 4.2.3 flagged none and labelled none redundant, contradictory or order-dependent.</p>
      ) : (
        <div className="rounded-token border bg-surface-raised">
          <Table label="Pairs that need a look">
            <thead>
              <tr>
                <Th>Pair</Th>
                <Th>State</Th>
                <Th>What 4.2.3 found</Th>
              </tr>
            </thead>
            <tbody>
              {attention.map((cell) => (
                <Tr key={cell.key}>
                  <Td className="whitespace-nowrap font-mono text-xs tabular-nums">
                    <button
                      type="button"
                      onClick={() => onSelect(cell.a.id, cell.b.id)}
                      className="rounded-token text-accent hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                    >
                      {position(ad, cell.a)} · {position(ad, cell.b)}
                    </button>
                  </Td>
                  <Td>
                    <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-sm">
                      <Swatch state={cell.state} />
                      {STATE[cell.state].label}
                    </span>
                  </Td>
                  <Td className="max-w-md whitespace-normal text-sm text-fg-muted">{findings(cell.found)}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </div>
      )}
    </section>
  );
}

function Swatch({ state }: { state: CellState }) {
  const { icon: Icon, cell, ink } = STATE[state];
  return (
    <span aria-hidden className={cn("inline-flex size-4 items-center justify-center rounded-sm border", cell)}>
      {Icon ? <Icon className={cn("size-3", ink)} strokeWidth={2.5} /> : null}
    </span>
  );
}

function Grid({
  title,
  rows,
  rowLabels,
  colLabels,
  chosen,
  ad,
  onSelect,
  onReadout,
}: {
  title: string;
  rows: Cell[][];
  rowLabels: string[];
  colLabels: string[];
  chosen: string | null;
  ad: StudioAd;
  onSelect: (a: string, b: string) => void;
  onReadout: (cell: Cell | null) => void;
}) {
  const [active, setActive] = useState<[number, number]>([0, 0]);
  const width = Math.max(0, ...rows.map((row) => row.length));
  if (rows.length === 0 || width === 0) return null;

  const move = (event: KeyboardEvent<HTMLTableElement>) => {
    const [r, c] = active;
    const at = (row: number, col: number): [number, number] => {
      const nextRow = Math.min(Math.max(row, 0), rows.length - 1);
      const length = rows[nextRow]?.length ?? 1;
      return [nextRow, Math.min(Math.max(col, 0), length - 1)];
    };
    const next: Record<string, [number, number]> = {
      ArrowUp: at(r - 1, c),
      ArrowDown: at(r + 1, c),
      ArrowLeft: at(r, c - 1),
      ArrowRight: at(r, c + 1),
      Home: at(r, 0),
      End: at(r, width),
    };
    const target = next[event.key];
    if (!target) return;
    event.preventDefault();
    setActive(target);
    const button = event.currentTarget.querySelector<HTMLButtonElement>(
      `[data-cell="${target[0]}-${target[1]}"]`,
    );
    button?.focus();
  };

  return (
    <div className="flex min-w-0 flex-col gap-2">
      <h3 className="text-xs font-medium text-fg-muted">{title}</h3>
      <div className="relative overflow-x-auto">
        <table role="grid" aria-label={`${title} pairs`} onKeyDown={move} className="border-separate border-spacing-0.5">
          <thead>
            <tr>
              <th scope="col">
                <span className="sr-only">Headline</span>
              </th>
              {colLabels.slice(0, width).map((label) => (
                <th key={label} scope="col" className="w-7 pb-1 text-center font-mono text-xs font-normal tabular-nums text-fg-muted">
                  {label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, r) => (
              <tr key={rowLabels[r]}>
                <th scope="row" className="pr-1.5 text-right font-mono text-xs font-normal tabular-nums text-fg-muted">
                  {rowLabels[r]}
                </th>
                {row.map((cell, c) => {
                  const { icon: Icon, cell: look, ink, label } = STATE[cell.state];
                  const isActive = active[0] === r && active[1] === c;
                  const isChosen = chosen === cell.key;
                  return (
                    <td key={cell.key} role="gridcell" aria-selected={isChosen}>
                      <button
                        type="button"
                        data-cell={`${r}-${c}`}
                        data-state={cell.state}
                        data-pair={`${cell.a.id}|${cell.b.id}`}
                        tabIndex={isActive ? 0 : -1}
                        aria-label={`${position(ad, cell.a)} with ${position(ad, cell.b)}: ${label}. ${findings(cell.found)}`}
                        onClick={() => {
                          setActive([r, c]);
                          onSelect(cell.a.id, cell.b.id);
                        }}
                        onFocus={() => onReadout(cell)}
                        onMouseEnter={() => onReadout(cell)}
                        className={cn(
                          "flex size-7 items-center justify-center rounded-sm border transition-transform duration-150 motion-reduce:transition-none",
                          "hover:scale-110 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
                          look,
                          isChosen && "outline-2 outline-offset-2 outline-fg",
                        )}
                      >
                        {Icon ? <Icon aria-hidden className={cn("size-3.5", ink)} strokeWidth={2.5} /> : null}
                      </button>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
