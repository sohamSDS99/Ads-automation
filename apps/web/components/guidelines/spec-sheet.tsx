"use client";

import { Ruler } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import type { AssetSpec, AssetSpecSheet } from "@/lib/api/guidelines";

/**
 * Google's asset limits, campaign type × asset type (PRD §15.3 E, §15.2).
 *
 * The design decision here is the one C15 asks for and it is the reason this
 * is a component rather than a table inline in the rulebook: **every number
 * carries where it came from and when anybody last checked it**, in the cell,
 * not in a footnote.
 *
 * Seed constants ship as `source: unverified` until a human reviews them
 * (C15, Q7). A sheet that rendered a guess and a checked figure identically
 * would be the failure mode the flag exists to prevent — so an unverified cell
 * says so on its face, and it is the only thing in this table drawn in a
 * colour.
 */
export function SpecSheet({
  sheet,
  scope,
}: {
  sheet?: AssetSpecSheet;
  scope?: "scoped" | "unscoped";
}) {
  const campaignTypes = Object.keys(sheet ?? {}).sort();
  // Every asset type that appears under any campaign type, so the matrix has a
  // stable column set and a gap reads as "not applicable here" rather than as
  // a different table shape per row.
  const assetTypes = [
    ...new Set(campaignTypes.flatMap((type) => Object.keys(sheet?.[type] ?? {}))),
  ].sort();

  if (campaignTypes.length === 0 || assetTypes.length === 0) {
    return (
      <EmptyState
        icon={Ruler}
        title="No asset specs yet"
        description="3.4.1 derives these from content_constants.yaml once it runs. A run bound to a frozen plan emits only the slate's campaign types; an unbound one emits every type."
      />
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-fg-muted">
        {scope === "scoped" ? (
          <>
            Narrowed to the campaign types in the plan this run was bound to. Types outside the
            slate are absent because nothing plans to use them, not because they have no limits.
          </>
        ) : (
          <>
            Every campaign type, because this run was not bound to a plan. An unbound run widens
            scope rather than guessing at one (law 21).
          </>
        )}
      </p>

      {/* Asset types down, campaign types across — not the other way round.
          Google has a dozen asset types and six campaign types, so the long
          axis has to be the rows: with assets as columns the fifth one is
          already scrolled out of sight on a 1440 screen, and a matrix whose
          right-hand columns are invisible reads as a matrix with data
          missing. §15.3 E asks for campaign type × asset type; which axis is
          which is this file's call. */}
      <Table label="Asset specifications by asset type and campaign type">
        <thead>
          <Tr>
            <Th>Asset type</Th>
            {campaignTypes.map((campaign) => (
              <Th key={campaign}>{humanise(campaign)}</Th>
            ))}
          </Tr>
        </thead>
        <tbody>
          {assetTypes.map((asset) => (
            <Tr key={asset}>
              <Td className="font-medium whitespace-nowrap">{humanise(asset)}</Td>
              {campaignTypes.map((campaign) => (
                <Td key={campaign} className="align-top">
                  <SpecCell spec={sheet?.[campaign]?.[asset]} />
                </Td>
              ))}
            </Tr>
          ))}
        </tbody>
      </Table>
    </div>
  );
}

/**
 * One cell: the limits, then their provenance.
 *
 * An absent spec is an em dash — this asset type does not apply to this
 * campaign type — and is deliberately distinguishable from a spec that exists
 * and happens to have no limits, which renders "no limits recorded".
 */
function SpecCell({ spec }: { spec?: AssetSpec }) {
  if (!spec) return <span className="text-fg-subtle">—</span>;

  const limits = [
    spec.max_chars != null ? `${spec.max_chars} chars` : null,
    spec.min_count != null && spec.max_count != null
      ? `${spec.min_count}–${spec.max_count}`
      : spec.max_count != null
        ? `up to ${spec.max_count}`
        : spec.min_count != null
          ? `${spec.min_count}+`
          : null,
    spec.ratio,
    spec.min_px ? `min ${spec.min_px}` : null,
    spec.max_bytes != null ? bytes(spec.max_bytes) : null,
  ].filter(Boolean) as string[];

  const unverified = spec.source === "unverified" || spec.source.startsWith("unverified");

  return (
    <div className="flex min-w-32 flex-col gap-1">
      <span data-numeric className="text-fg">
        {limits.length > 0 ? limits.join(" · ") : "no limits recorded"}
      </span>
      {/* Provenance, always visible. The `title` repeats it for the constants
          key itself, which is long and would otherwise wrap the column. */}
      {unverified ? (
        // `self-start` or the badge stretches to the column width and reads
        // as a filled cell rather than a chip.
        <span className="self-start">
          <Badge tone="warning">unverified</Badge>
        </span>
      ) : (
        <span className="text-xs text-fg-subtle" title={spec.source}>
          {shortSource(spec.source)}
          {" · "}
          <time dateTime={spec.reviewed_at}>{spec.reviewed_at}</time>
        </span>
      )}
      {unverified ? (
        <span className="text-xs text-fg-subtle" title={spec.source}>
          nobody has checked this against Google yet
        </span>
      ) : null}
    </div>
  );
}

/**
 * The tail of a `content_constants.yaml` key.
 *
 * `asset_specs.search.rsa_headline.max_chars` in a table cell wraps to three
 * lines and tells a reader nothing the row and column have not already said.
 * The full key stays in the `title` and in the export.
 */
function shortSource(source: string): string {
  if (source.startsWith("http")) return "Google";
  const parts = source.split(".");
  return parts.length > 2 ? parts.slice(-2).join(".") : source;
}

/** `5 MB`, not `5120 KB` — the unit a person would have said out loud. */
function bytes(value: number): string {
  if (value >= 1024 * 1024) {
    const mb = value / (1024 * 1024);
    return `${Number.isInteger(mb) ? mb : mb.toFixed(1)} MB`;
  }
  return `${Math.round(value / 1024)} KB`;
}

function humanise(value: string): string {
  return value.replace(/_/g, " ");
}
