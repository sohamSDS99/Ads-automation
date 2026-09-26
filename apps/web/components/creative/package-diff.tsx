"use client";

import { ArrowRight, Minus, Plus } from "lucide-react";
import { Fragment } from "react";

import { MonoId } from "@/components/creative/mono-id";
import { PackageSection } from "@/components/creative/package-sections";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import type { DiffAsset, DiffChange, PackageDiff as Diff, PackageMediaRendition } from "@/lib/api/creative-packages";
import { mediaContentUrl } from "@/lib/api/media-library";
import { changedFields, fieldText } from "@/lib/creative/package";
import { wordDiff, type DiffToken } from "@/lib/creative/word-diff";
import { cn } from "@/lib/utils";

/** `c-sds-us/sds software/headline/search_headline/A` → the words a person reads. */
function slotLabel(slot: string, kind: string): string {
  const parts = slot.split("/");
  const media = kind === "image" || kind === "video" || kind === "logo";
  if (media) {
    const [campaign, modality, concept] = parts;
    return `${campaign} · ${modality}${concept && concept !== "-" ? ` · ${concept}` : ""}`;
  }
  const [campaign, adGroup, , surface, variant] = parts;
  return [campaign, adGroup !== "-" ? adGroup : null, surface?.replace(/_/g, " "), variant && variant !== "-" ? `variant ${variant}` : null]
    .filter(Boolean)
    .join(" · ");
}

function isMedia(asset: DiffAsset): boolean {
  return asset.renditions.length > 0 || asset.kind === "image" || asset.kind === "video" || asset.kind === "logo";
}

/** Tokens only one side has are struck (before) or underlined (after) — and named for a screen reader. */
function Tokens({ tokens, side }: { tokens: DiffToken[]; side: "before" | "after" }) {
  return (
    <>
      {tokens.map((token, index) => (
        <Fragment key={index}>
          {index > 0 ? " " : null}
          {token.kind === "only" ? (
            side === "before" ? (
              <del className="decoration-status-failed-ink decoration-2" data-testid="diff-removed-word">
                {token.text}
              </del>
            ) : (
              <ins className="font-medium underline decoration-status-success decoration-2 underline-offset-4" data-testid="diff-added-word">
                {token.text}
              </ins>
            )
          ) : (
            <span>{token.text}</span>
          )}
        </Fragment>
      ))}
    </>
  );
}

function TextChange({ change }: { change: DiffChange }) {
  const before = change.from.text ?? "";
  const after = change.to.text ?? "";
  const words = wordDiff(before, after);
  const rows = changedFields(change.from.fields, change.to.fields);
  return (
    <div className="flex flex-col gap-2">
      {before || after ? (
        <div className="grid gap-2 sm:grid-cols-2" data-testid="text-diff">
          <p className="rounded-token border px-3 py-2 text-sm text-fg">
            <span className="block text-xs text-fg-subtle">Before</span>
            {before ? <Tokens tokens={words.a} side="before" /> : <span className="text-fg-muted">No text</span>}
          </p>
          <p className="rounded-token border border-border-strong px-3 py-2 text-sm text-fg">
            <span className="block text-xs text-fg-subtle">After</span>
            {after ? <Tokens tokens={words.b} side="after" /> : <span className="text-fg-muted">No text</span>}
          </p>
        </div>
      ) : null}
      {rows.length > 0 ? (
        <ul className="flex flex-col divide-y rounded-token border text-sm" data-testid="field-diff">
          {rows.map((row) => (
            <li key={row.field} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-3 py-2" data-testid="field-change">
              <span className="min-w-32 text-fg-muted">{row.field}</span>
              <span className="inline-flex min-w-0 flex-wrap items-baseline gap-x-2 tabular-nums">
                <del className="break-all decoration-status-failed-ink decoration-2">{row.before}</del>
                <ArrowRight className="size-3.5 shrink-0 translate-y-0.5 text-fg-subtle" aria-label="changed to" />
                <ins className="break-all font-medium underline decoration-status-success decoration-2 underline-offset-4">
                  {row.after}
                </ins>
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function Rendition({ rendition, side }: { rendition: PackageMediaRendition | undefined; side: "Before" | "After" }) {
  if (!rendition) {
    return (
      <div className="flex min-h-24 items-center justify-center rounded-token border border-dashed px-3 py-6 text-sm text-fg-muted">
        {side === "Before" ? "Not in the earlier version" : "Not in this version"}
      </div>
    );
  }
  const video = rendition.media_type.startsWith("video/");
  return (
    <figure className="flex min-w-0 flex-col gap-1.5" data-testid="diff-media" data-side={side.toLowerCase()}>
      {/* eslint-disable-next-line @next/next/no-img-element -- a stored rendition streamed by api */}
      <img
        src={mediaContentUrl(rendition.media_id, video ? "poster" : "preview")}
        alt={`${side}: ${rendition.surface.replace(/_/g, " ")} at ${rendition.aspect_ratio}, ${rendition.width}×${rendition.height}`}
        width={rendition.width}
        height={rendition.height}
        loading="lazy"
        className="h-auto max-h-64 w-full rounded-token border bg-surface object-contain"
      />
      <figcaption className="flex flex-wrap items-center gap-x-2 text-xs text-fg-muted tabular-nums">
        <span className="font-medium text-fg">{side}</span>
        {rendition.aspect_ratio} · {rendition.width}×{rendition.height}
        <MonoId value={rendition.sha256} label={`${side.toLowerCase()} sha256`} />
      </figcaption>
    </figure>
  );
}

/** Before and after, side by side, one row per aspect ratio either side ships. */
function MediaChange({ from, to }: { from: DiffAsset | null; to: DiffAsset | null }) {
  const ratios = [...new Set([...(from?.renditions ?? []), ...(to?.renditions ?? [])].map((r) => `${r.surface}@${r.aspect_ratio}`))];
  return (
    <div className="flex flex-col gap-3" data-testid="media-diff">
      {ratios.map((key) => {
        const [surface, ratio] = key.split("@");
        const find = (asset: DiffAsset | null) => asset?.renditions.find((r) => r.surface === surface && r.aspect_ratio === ratio);
        return (
          <div key={key} className="grid grid-cols-2 gap-3">
            <Rendition rendition={find(from)} side="Before" />
            <Rendition rendition={find(to)} side="After" />
          </div>
        );
      })}
    </div>
  );
}

function AssetOnly({ asset, kind }: { asset: DiffAsset; kind: "added" | "removed" }) {
  if (isMedia(asset)) {
    return kind === "added" ? <MediaChange from={null} to={asset} /> : <MediaChange from={asset} to={null} />;
  }
  const fields = Object.entries(asset.fields);
  return (
    <p className={cn("rounded-token border px-3 py-2 text-sm text-fg", kind === "removed" && "text-fg-muted")}>
      {kind === "removed" ? <del className="decoration-status-failed-ink decoration-2">{asset.text ?? "—"}</del> : asset.text ?? "—"}
      {fields.length > 0 && !asset.text ? (
        <span className="block font-mono text-xs text-fg-muted">
          {fields.map(([key, value]) => `${key.replace(/_/g, " ")}: ${fieldText(value)}`).join(" · ")}
        </span>
      ) : null}
    </p>
  );
}

/**
 * `PackageDiff` — what the later package added, removed and changed against
 * the earlier one (Stage 04 PRD §15.4 L), as `package_diff` matched them by
 * slot. Changed text is shown before beside after, word by word, the words
 * only one side has struck through or underlined; changed media is shown side
 * by side, each aspect ratio against its counterpart. The pins that moved are
 * listed first. Nothing is matched here.
 */
export function PackageDiff({ diff, beforeLabel, afterLabel }: { diff: Diff; beforeLabel: string; afterLabel: string }) {
  const empty = diff.changed.length === 0 && diff.added.length === 0 && diff.removed.length === 0 && diff.pins.length === 0;
  return (
    <div className="flex flex-col gap-8" data-testid="package-diff">
      <p className="flex flex-wrap items-center gap-2 text-sm text-fg-muted tabular-nums" data-testid="diff-summary">
        <span className="font-medium text-fg">{beforeLabel}</span>
        <ArrowRight className="size-4" aria-hidden />
        <span className="font-medium text-fg">{afterLabel}</span>· {diff.changed.length} changed · {diff.added.length} added ·{" "}
        {diff.removed.length} removed · {diff.pins.length} {diff.pins.length === 1 ? "pin" : "pins"} moved
      </p>

      {empty ? (
        <p className="rounded-token border px-4 py-6 text-center text-sm text-fg-muted">
          The two packages ship the same copy and media against the same pins.
        </p>
      ) : null}

      {diff.pins.length > 0 ? (
        <PackageSection id="diff-pins" title="Pins">
          <div className="rounded-token border">
            <Table label="Pins that moved" className="min-w-0">
              <thead>
                <Tr>
                  <Th>Pin</Th>
                  <Th className="w-1/2">Before</Th>
                  <Th className="w-1/2">After</Th>
                </Tr>
              </thead>
              <tbody>
                {diff.pins.map((pin) => (
                  <Tr key={pin.field}>
                    <Td className="text-fg-muted">{pin.field.replace(/_/g, " ")}</Td>
                    <Td className="max-w-0 truncate font-mono text-xs" title={pin.before}>
                      {pin.before}
                    </Td>
                    <Td className="max-w-0 truncate font-mono text-xs font-medium" title={pin.after}>
                      {pin.after}
                    </Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          </div>
        </PackageSection>
      ) : null}

      {diff.changed.length > 0 ? (
        <PackageSection id="diff-changed" title="Changed" meta={<span className="text-sm text-fg-muted tabular-nums">{diff.changed.length}</span>}>
          <ol className="flex flex-col gap-6">
            {diff.changed.map((change, index) => (
              <li key={`${change.slot}-${index}`} className="flex flex-col gap-2" data-testid="diff-change" data-kind={change.kind}>
                <p className="text-sm">
                  <span className="font-medium text-fg">{change.kind.replace(/_/g, " ")}</span>
                  <span className="text-fg-muted"> · {slotLabel(change.slot, change.kind)}</span>
                </p>
                {isMedia(change.to) || isMedia(change.from) ? (
                  <MediaChange from={change.from} to={change.to} />
                ) : (
                  <TextChange change={change} />
                )}
              </li>
            ))}
          </ol>
        </PackageSection>
      ) : null}

      {(["added", "removed"] as const).map((kind) => {
        const items = diff[kind];
        if (items.length === 0) return null;
        const Icon = kind === "added" ? Plus : Minus;
        return (
          <PackageSection
            key={kind}
            id={`diff-${kind}`}
            title={kind === "added" ? "Added" : "Removed"}
            meta={<span className="text-sm text-fg-muted tabular-nums">{items.length}</span>}
          >
            <ul className="flex flex-col gap-4">
              {items.map((asset) => (
                <li key={asset.asset_id} className="flex flex-col gap-2" data-testid={`diff-${kind}-item`}>
                  <p className="flex items-center gap-1.5 text-sm">
                    <Icon className="size-3.5 text-fg-subtle" aria-hidden />
                    <span className="font-medium text-fg">{asset.kind.replace(/_/g, " ")}</span>
                    <span className="text-fg-muted">· {slotLabel(asset.slot, asset.kind)}</span>
                  </p>
                  <AssetOnly asset={asset} kind={kind} />
                </li>
              ))}
            </ul>
          </PackageSection>
        );
      })}
    </div>
  );
}
