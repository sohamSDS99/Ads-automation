"use client";

import { ChevronDown, ChevronRight, Eye, Pin, ShieldCheck } from "lucide-react";
import { useEffect, useId, useState } from "react";

import { CharCounter } from "@/components/creative/char-counter";
import { CopyText, SwapMenu, useCopyEdit, type LintScope } from "@/components/creative/copy-edit";
import { LintChip } from "@/components/creative/lint-chip";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { ApiError } from "@/lib/api";
import type { ClaimRef, CreativeAssetItem } from "@/lib/api/creative-runs";
import { position, type StudioAd } from "@/lib/creative/ad-studio";
import { charCount } from "@/lib/creative/char-count";
import { useSwapCreativeAsset } from "@/lib/queries";
import { cn } from "@/lib/utils";

const CATEGORY: Record<string, string> = {
  keyword: "Keyword",
  benefit: "Benefit",
  offer: "Offer",
  proof: "Proof",
  objection: "Objection",
  cta: "Call to action",
};

export function categoryLabel(category: string | null): string {
  return category ? (CATEGORY[category] ?? category) : "—";
}

/**
 * `HeadlineMatrix` — the ad's headlines as a table, one row each (Stage 04
 * PRD §15.4 E, §15.2 rule 4: fifteen headlines are a table, never fifteen
 * cards): the text, editable in place; its category; its `CharCounter`
 * against the pinned limit; its pin; its `LintChip`; the claims it stands on.
 * Above it, one quota bar per category — what 4.2.1 selected to beside what
 * the ad carries now. Beneath it, the reserves, collapsed, each swappable in
 * by keyboard.
 */
export function HeadlineMatrix({
  runId,
  ad,
  limit,
  claims,
  scope,
  editable,
  onPreview,
}: {
  runId: string;
  ad: StudioAd;
  limit: number | null;
  claims: Map<string, ClaimRef>;
  scope: LintScope;
  editable: boolean;
  /** Load one headline into the SERP preview. */
  onPreview: (assetId: string) => void;
}) {
  const reservesId = useId();
  const [open, setOpen] = useState(false);
  const [focusId, setFocusId] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const swap = useSwapCreativeAsset(runId);
  const labelOf = (asset: CreativeAssetItem) => position(ad, asset);

  // After a swap the swapped-in headline is where the keyboard continues.
  useEffect(() => {
    if (focusId === null) return;
    const target = document.getElementById(`copy-${focusId}`) ?? document.getElementById(`row-${focusId}`);
    if (target) {
      target.focus();
      setFocusId(null);
    }
  }, [focusId, ad.headlines]);

  const onSwap = (reserve: CreativeAssetItem, out: CreativeAssetItem) => {
    const where = labelOf(out);
    swap.mutate(
      { assetId: out.id, reserveId: reserve.id },
      {
        onSuccess: () => {
          setAnnouncement(`Swapped “${reserve.text}” into ${where} in place of “${out.text}”.`);
          setFocusId(reserve.id);
        },
      },
    );
  };

  return (
    <section aria-labelledby="headline-matrix-title" className="flex min-w-0 flex-col gap-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 id="headline-matrix-title" className="text-sm font-medium text-fg">
          Headlines
        </h2>
        <p className="text-xs tabular-nums text-fg-muted">
          {ad.headlines.length} in the ad · {ad.headlineReserves.length} in reserve
          {limit !== null ? ` · ${limit} characters each` : ""}
        </p>
      </div>

      <QuotaBars quota={ad.quota} />

      <div className="rounded-token border bg-surface-raised">
        <Table label={`Headlines of ${ad.slot.ad_group_ref}, variant ${ad.variant}`}>
          <thead>
            <tr>
              <Th className="w-10 px-3">#</Th>
              <Th className="px-3">Headline</Th>
              <Th className="px-3">Category</Th>
              <Th className="px-3 text-right">Length</Th>
              <Th className="px-3">Pin</Th>
              <Th className="px-3">Lint</Th>
              <Th className="px-2">
                <span className="sr-only">Preview</span>
              </Th>
            </tr>
          </thead>
          <tbody>
            {ad.headlines.map((asset) => (
              <HeadlineRow
                key={asset.id}
                runId={runId}
                asset={asset}
                label={labelOf(asset)}
                limit={limit}
                claims={claims}
                scope={scope}
                editable={editable && asset.frozen_at === null}
                onPreview={onPreview}
              />
            ))}
          </tbody>
        </Table>
      </div>

      <div aria-live="polite" className="sr-only">
        {announcement}
      </div>

      {ad.headlineReserves.length > 0 ? (
        <div className="flex flex-col gap-2">
          <button
            type="button"
            aria-expanded={open}
            aria-controls={reservesId}
            onClick={() => setOpen((value) => !value)}
            className="inline-flex w-fit items-center gap-1.5 rounded-token text-sm text-fg-muted hover:text-fg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          >
            {open ? <ChevronDown className="size-4" aria-hidden /> : <ChevronRight className="size-4" aria-hidden />}
            {open ? "Hide" : "Show"} {ad.headlineReserves.length} reserve headlines
          </button>
          {swap.error ? (
            <Alert tone="error" title="The swap was not made">
              {swap.error instanceof ApiError ? swap.error.detail : "Check your connection and try again."}
            </Alert>
          ) : null}
          <div id={reservesId} hidden={!open} className="rounded-token border bg-surface">
            <Table label={`Reserve headlines of ${ad.slot.ad_group_ref}, variant ${ad.variant}`}>
              <thead>
                <tr>
                  <Th className="px-3">Reserve</Th>
                  <Th className="px-3">Category</Th>
                  <Th className="px-3 text-right">Length</Th>
                  <Th className="px-3">Lint</Th>
                  {editable ? (
                    <Th className="px-3">
                      <span className="sr-only">Swap</span>
                    </Th>
                  ) : null}
                </tr>
              </thead>
              <tbody>
                {ad.headlineReserves.map((reserve) => (
                  <Tr key={reserve.id} data-testid={`reserve-${reserve.id}`}>
                    <Td className="max-w-80 whitespace-normal px-3">
                      <div className="flex flex-col gap-1">
                        {reserve.text}
                        {reserve.claim_ids.length > 0 ? <ClaimChips ids={reserve.claim_ids} claims={claims} /> : null}
                      </div>
                    </Td>
                    <Td className="px-3">
                      <Badge>{categoryLabel(reserve.category)}</Badge>
                    </Td>
                    <Td className="px-3 text-right">
                      <CharCounter count={countOf(reserve)} limit={limit} />
                    </Td>
                    <Td className="px-3">
                      <LintChip verdict={reserve.lint_verdict} />
                    </Td>
                    {editable ? (
                      <Td className="px-3">
                        {reserve.frozen_at === null ? (
                          <SwapMenu
                            reserve={reserve}
                            carried={ad.headlines.filter((asset) => asset.frozen_at === null)}
                            labelOf={labelOf}
                            categoryOf={(asset) => categoryLabel(asset.category)}
                            onSwap={(out) => onSwap(reserve, out)}
                            busy={swap.isPending}
                          />
                        ) : null}
                      </Td>
                    ) : null}
                  </Tr>
                ))}
              </tbody>
            </Table>
          </div>
        </div>
      ) : null}
    </section>
  );
}

function HeadlineRow({
  runId,
  asset,
  label,
  limit,
  claims,
  scope,
  editable,
  onPreview,
}: {
  runId: string;
  asset: CreativeAssetItem;
  label: string;
  limit: number | null;
  claims: Map<string, ClaimRef>;
  scope: LintScope;
  editable: boolean;
  onPreview: (assetId: string) => void;
}) {
  const edit = useCopyEdit(runId, asset, scope);
  return (
    <Tr id={`row-${asset.id}`} tabIndex={-1} data-testid={`headline-${asset.id}`} className="focus:outline-none">
      <Td className="px-3 font-mono text-xs tabular-nums text-fg-muted">{label}</Td>
      <Td className="w-64 min-w-60 px-3 py-2">
        <CopyText
          id={`copy-${asset.id}`}
          label={`${label} headline`}
          edit={edit}
          editable={editable}
          below={asset.claim_ids.length > 0 ? <ClaimChips ids={asset.claim_ids} claims={claims} /> : null}
        />
      </Td>
      <Td className="px-3">
        <Badge>{categoryLabel(asset.category)}</Badge>
      </Td>
      <Td className="px-3 text-right">
        <CharCounter count={edit.count} limit={limit} />
      </Td>
      <Td className="px-3">
        {asset.pin_position ? (
          <Badge tone="accent" className="whitespace-nowrap">
            <Pin className="size-3" aria-hidden />
            <span title={`Pinned to headline position ${asset.pin_position.slice(1)}`}>
              Pin {asset.pin_position.slice(1)}
            </span>
          </Badge>
        ) : (
          <span className="text-xs text-fg-muted">
            <span aria-hidden>—</span>
            <span className="sr-only">Not pinned</span>
          </span>
        )}
      </Td>
      <Td className="px-3">
        <LintChip verdict={edit.verdict} pending={edit.pending} />
      </Td>
      <Td className="px-2">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => onPreview(asset.id)}
          aria-label={`Show ${label} in the preview`}
        >
          <Eye aria-hidden />
        </Button>
      </Td>
    </Tr>
  );
}

/** The parity-tested count, never a second one. */
function countOf(asset: CreativeAssetItem): number {
  return charCount(asset.surface, asset.text ?? "");
}

export function ClaimChips({ ids, claims }: { ids: string[]; claims: Map<string, ClaimRef> }) {
  if (ids.length === 0) {
    return (
      <span className="text-xs text-fg-muted">
        <span aria-hidden>—</span>
        <span className="sr-only">No claim</span>
      </span>
    );
  }
  return (
    <span className="flex flex-wrap gap-1">
      {ids.map((id) => {
        const claim = claims.get(id);
        return (
          <Badge key={id} className="max-w-56">
            <ShieldCheck className="size-3 shrink-0" aria-hidden />
            <span className="truncate" title={claim ? `${claim.normalized_text} · claim ${id}` : `Claim ${id}`}>
              {claim ? claim.normalized_text : `Claim ${id.slice(0, 4)}…${id.slice(-4)}`}
            </span>
          </Badge>
        );
      })}
    </span>
  );
}

/**
 * One bar per category: a segment for each headline 4.2.1's quota asks for,
 * filled for each the ad carries now — and the numbers, so the bar is never
 * read by colour alone. Short of the quota is said in words.
 */
function QuotaBars({ quota }: { quota: StudioAd["quota"] }) {
  return (
    <ul aria-label="Headline quotas by category" className="flex flex-wrap gap-x-6 gap-y-2">
      {quota.map((line) => {
        const short = line.required - line.carried;
        const segments = Math.max(line.required, line.carried);
        return (
          <li key={line.category} className="flex items-center gap-2 text-xs">
            <span className="w-24 text-fg-muted">{categoryLabel(line.category)}</span>
            <span aria-hidden className="flex gap-0.5">
              {Array.from({ length: segments }, (_, index) => (
                <span
                  key={index}
                  className={cn(
                    "h-2 w-3 rounded-full border",
                    index < line.carried
                      ? short > 0
                        ? "border-status-failed-ink bg-status-failed-ink"
                        : "border-fg-muted bg-fg-muted"
                      : "border-border-strong bg-transparent",
                  )}
                />
              ))}
            </span>
            <span className={cn("tabular-nums", short > 0 ? "font-medium text-status-failed-ink" : "text-fg")}>
              {line.carried}/{line.required}
              {short > 0 ? ` · ${short} short` : ""}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
