"use client";

import { ChevronDown, ChevronRight, Pin, ShieldCheck } from "lucide-react";
import { useEffect, useId, useState } from "react";

import { CharCounter } from "@/components/creative/char-counter";
import { CopyText, SwapMenu, useCopyEdit, type LintScope } from "@/components/creative/copy-edit";
import { LintChip } from "@/components/creative/lint-chip";
import { MonoId } from "@/components/creative/mono-id";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { ApiError } from "@/lib/api";
import type { ClaimRef, CreativeAssetItem } from "@/lib/api/creative-runs";
import { position, type StudioAd } from "@/lib/creative/ad-studio";
import { charCount } from "@/lib/creative/char-count";
import { useSwapCreativeAsset } from "@/lib/queries";

/**
 * `DescriptionTable` — the ad's descriptions, each beside the licensed claim
 * it is bound to (Stage 04 PRD §15.4 E, law 34). The words that state the
 * claim are marked in the text where 4.2.2 — or the server, after an edit —
 * found them (`fields.claim_span`); an edit that drops them is refused by the
 * server, which names the words to keep.
 */
export function DescriptionTable({
  runId,
  ad,
  limit,
  claims,
  scope,
  editable,
}: {
  runId: string;
  ad: StudioAd;
  limit: number | null;
  claims: Map<string, ClaimRef>;
  scope: LintScope;
  editable: boolean;
}) {
  const reservesId = useId();
  const [open, setOpen] = useState(false);
  const [focusId, setFocusId] = useState<string | null>(null);
  const swap = useSwapCreativeAsset(runId);
  const labelOf = (asset: CreativeAssetItem) => position(ad, asset);

  useEffect(() => {
    if (focusId === null) return;
    const target = document.getElementById(`copy-${focusId}`);
    if (target) {
      target.focus();
      setFocusId(null);
    }
  }, [focusId, ad.descriptions]);

  return (
    <section aria-labelledby="description-table-title" className="flex min-w-0 flex-col gap-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 id="description-table-title" className="text-sm font-medium text-fg">
          Descriptions
        </h2>
        <p className="text-xs tabular-nums text-fg-muted">
          {ad.descriptions.length} in the ad · {ad.descriptionReserves.length} in reserve
          {limit !== null ? ` · ${limit} characters each` : ""}
        </p>
      </div>
      <div className="rounded-token border bg-surface-raised">
        <Table label={`Descriptions of ${ad.slot.ad_group_ref}, variant ${ad.variant}`}>
          <thead>
            <tr>
              <Th className="w-12">#</Th>
              <Th>Description</Th>
              <Th className="text-right">Length</Th>
              <Th>Pin</Th>
              <Th>Lint</Th>
              <Th>Bound claim</Th>
            </tr>
          </thead>
          <tbody>
            {ad.descriptions.map((asset) => (
              <DescriptionRow
                key={asset.id}
                runId={runId}
                asset={asset}
                label={labelOf(asset)}
                limit={limit}
                claims={claims}
                scope={scope}
                editable={editable && asset.frozen_at === null}
              />
            ))}
          </tbody>
        </Table>
      </div>

      {ad.descriptionReserves.length > 0 ? (
        <div className="flex flex-col gap-2">
          <button
            type="button"
            aria-expanded={open}
            aria-controls={reservesId}
            onClick={() => setOpen((value) => !value)}
            className="inline-flex w-fit items-center gap-1.5 rounded-token text-sm text-fg-muted hover:text-fg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          >
            {open ? <ChevronDown className="size-4" aria-hidden /> : <ChevronRight className="size-4" aria-hidden />}
            {open ? "Hide" : "Show"} {ad.descriptionReserves.length} reserve{" "}
            {ad.descriptionReserves.length === 1 ? "description" : "descriptions"}
          </button>
          {swap.error ? (
            <Alert tone="error" title="The swap was not made">
              {swap.error instanceof ApiError ? swap.error.detail : "Check your connection and try again."}
            </Alert>
          ) : null}
          <div id={reservesId} hidden={!open} className="rounded-token border bg-surface">
            <Table label={`Reserve descriptions of ${ad.slot.ad_group_ref}, variant ${ad.variant}`}>
              <thead>
                <tr>
                  <Th>Reserve</Th>
                  <Th className="text-right">Length</Th>
                  <Th>Lint</Th>
                  <Th>Bound claim</Th>
                  {editable ? (
                    <Th>
                      <span className="sr-only">Swap</span>
                    </Th>
                  ) : null}
                </tr>
              </thead>
              <tbody>
                {ad.descriptionReserves.map((reserve) => (
                  <Tr key={reserve.id}>
                    <Td className="max-w-md whitespace-normal">
                      <ClaimSpan asset={reserve} />
                    </Td>
                    <Td className="text-right">
                      <CharCounter count={charCount(reserve.surface, reserve.text ?? "")} limit={limit} />
                    </Td>
                    <Td>
                      <LintChip verdict={reserve.lint_verdict} />
                    </Td>
                    <Td>
                      <BoundClaim ids={reserve.claim_ids} claims={claims} />
                    </Td>
                    {editable ? (
                      <Td>
                        {reserve.frozen_at === null ? (
                          <SwapMenu
                            reserve={reserve}
                            carried={ad.descriptions.filter((asset) => asset.frozen_at === null)}
                            labelOf={labelOf}
                            onSwap={(out) =>
                              swap.mutate(
                                { assetId: out.id, reserveId: reserve.id },
                                { onSuccess: () => setFocusId(reserve.id) },
                              )
                            }
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

function DescriptionRow({
  runId,
  asset,
  label,
  limit,
  claims,
  scope,
  editable,
}: {
  runId: string;
  asset: CreativeAssetItem;
  label: string;
  limit: number | null;
  claims: Map<string, ClaimRef>;
  scope: LintScope;
  editable: boolean;
}) {
  const edit = useCopyEdit(runId, asset, scope);
  return (
    <Tr data-testid={`description-${asset.id}`}>
      <Td className="font-mono text-xs tabular-nums text-fg-muted">{label}</Td>
      <Td className="py-2">
        <CopyText id={`copy-${asset.id}`} label={`${label} description`} edit={edit} editable={editable}>
          <ClaimSpan asset={asset} />
        </CopyText>
      </Td>
      <Td className="text-right">
        <CharCounter count={edit.count} limit={limit} />
      </Td>
      <Td>
        {asset.pin_position ? (
          <Badge tone="accent" className="whitespace-nowrap">
            <Pin className="size-3" aria-hidden />
            Pin {asset.pin_position.slice(1)}
          </Badge>
        ) : (
          <span className="text-xs text-fg-muted">
            <span aria-hidden>—</span>
            <span className="sr-only">Not pinned</span>
          </span>
        )}
      </Td>
      <Td>
        <LintChip verdict={edit.verdict} pending={edit.pending} />
      </Td>
      <Td>
        <BoundClaim ids={asset.claim_ids} claims={claims} />
      </Td>
    </Tr>
  );
}

/** The text, with the words that state its claim marked where the server found them. */
function ClaimSpan({ asset }: { asset: CreativeAssetItem }) {
  const text = asset.text ?? "";
  const span = asset.fields["claim_span"];
  if (!Array.isArray(span) || span.length !== 2) return <>{text}</>;
  const [start, end] = span as [number, number];
  // Offsets are the server's: Python string indices, i.e. code points.
  const points = Array.from(text);
  return (
    <>
      {points.slice(0, start).join("")}
      <mark className="rounded-sm bg-accent-soft px-0.5 text-fg">{points.slice(start, end).join("")}</mark>
      {points.slice(end).join("")}
    </>
  );
}

function BoundClaim({ ids, claims }: { ids: string[]; claims: Map<string, ClaimRef> }) {
  return (
    <ul className="flex max-w-72 flex-col gap-1">
      {ids.map((id) => {
        const claim = claims.get(id);
        return (
          <li key={id} className="flex flex-col gap-0.5 whitespace-normal">
            <span className="inline-flex items-start gap-1.5 text-sm text-fg">
              <ShieldCheck className="mt-0.5 size-3.5 shrink-0 text-status-success" aria-hidden />
              {claim ? claim.normalized_text : "A claim licensed at the run’s pin"}
            </span>
            <span className="text-xs text-fg-muted">
              <MonoId value={id} label="claim id" />
              {claim?.expires_at ? ` · expires ${day(claim.expires_at)}` : claim ? " · does not expire" : ""}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function day(iso: string): string {
  return new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" }).format(
    new Date(iso),
  );
}
