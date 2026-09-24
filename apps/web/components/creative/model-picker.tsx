"use client";

import Link from "next/link";

import { AspectGlyphRow } from "@/components/creative/aspect-glyph";
import { Alert } from "@/components/ui/alert";
import { Combobox, type ComboboxItem } from "@/components/ui/combobox";
import { Skeleton } from "@/components/ui/skeleton";
import {
  UNAVAILABLE_REASON,
  capabilityChips,
  priceLabel,
  sortedPrices,
  type MediaModality,
  type MediaModelRow,
  type MediaModelsResponse,
} from "@/lib/api/media";

/** A row's identity: the same model may be allowlisted once per provider pin. */
export function modelKey(row: Pick<MediaModelRow, "model_id" | "provider_tag">): string {
  return `${row.model_id}|${row.provider_tag ?? ""}`;
}

function nameOf(modelId: string): { vendor: string; name: string } {
  const slash = modelId.indexOf("/");
  return slash === -1
    ? { vendor: "", name: modelId }
    : { vendor: modelId.slice(0, slash), name: modelId.slice(slash + 1) };
}

/** `openai · any provider`, `black-forest-labs · pinned to black-forest-labs`. */
function providerLine(row: MediaModelRow): string {
  const { vendor } = nameOf(row.model_id);
  const route = row.provider_tag ? `pinned to ${row.provider_tag}` : "any provider";
  return vendor ? `${vendor} · ${route}` : route;
}

function Row({ row }: { row: MediaModelRow }) {
  const { name } = nameOf(row.model_id);
  const record = row.capability;
  const prices = record ? sortedPrices(record) : [];
  const [first, ...more] = prices;
  return (
    <span className="flex min-w-0 flex-col gap-1">
      <span className="flex min-w-0 items-baseline justify-between gap-3">
        <span className="truncate font-mono text-xs font-medium text-fg" title={row.model_id}>
          {name}
        </span>
        <span className="shrink-0 truncate text-xs text-fg-subtle">{providerLine(row)}</span>
      </span>
      {record ? (
        <span className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-fg-muted">
          {capabilityChips(record).map((chip) => (
            <span key={chip} className="rounded-full border px-1.5 py-px tabular-nums">
              {chip}
            </span>
          ))}
          {row.ratio_coverage ? <AspectGlyphRow coverage={row.ratio_coverage} size={14} /> : null}
          <span
            className="tabular-nums"
            title={prices.length > 1 ? prices.map(priceLabel).join("\n") : undefined}
          >
            {first ? priceLabel(first) : "No price listed"}
            {more.length > 0 ? ` · +${more.length} more` : ""}
          </span>
        </span>
      ) : null}
      {!row.available && row.reason ? (
        <span className="text-xs text-fg-muted">{UNAVAILABLE_REASON[row.reason]}</span>
      ) : null}
    </span>
  );
}

/**
 * `ModelPicker` — one image or one video model for this run, from the
 * allowlist ∩ the live catalogue (PRD §9.2, §15.4 B, law 36).
 *
 * A `cmdk` combobox: type to filter, arrows to move, Enter to choose. Each
 * row carries what the choice depends on — the model, its provider, its
 * capability chips, an `AspectGlyph` per ratio the spec sheet requires, and
 * its cheapest price line — so a person compares models without opening
 * each one. A model the server marks unavailable is still listed, disabled,
 * with the server's reason: an allowlisted model that silently vanished
 * would read as never having been allowed.
 *
 * Nothing is chosen for the person — IMAGE_GEN and VIDEO_GEN have no seed
 * default (law 36) — and nothing is swapped when a model drops out.
 */
export function ModelPicker({
  modality,
  id,
  models,
  pending,
  error,
  value,
  onChange,
}: {
  modality: MediaModality;
  id: string;
  models?: MediaModelsResponse;
  pending: boolean;
  error: string | null;
  value: string | null;
  onChange: (row: MediaModelRow) => void;
}) {
  if (error) {
    return (
      <Alert tone="error" title={`The ${modality} models could not be loaded`}>
        {error}
      </Alert>
    );
  }
  if (pending || !models) return <Skeleton className="h-9 w-full" />;

  if (models.models.length === 0) {
    return (
      <div className="rounded-token border border-dashed px-3 py-3 text-sm text-fg-muted">
        No {modality} model is allowlisted yet. An admin chooses them under{" "}
        <Link href="/settings/models" className="font-medium text-fg underline underline-offset-2">
          Settings → Models → Media generation
        </Link>
        ; until then, turn {modality === "image" ? "Images" : "Video"} off to start without it.
      </div>
    );
  }

  const items: ComboboxItem[] = models.models.map((row) => ({
    value: modelKey(row),
    search: `${row.model_id} ${row.provider_tag ?? ""}`,
    disabled: !row.available,
    render: <Row row={row} />,
    label: (
      <span className="flex min-w-0 items-baseline gap-2">
        <span className="truncate font-mono text-xs font-medium">{nameOf(row.model_id).name}</span>
        <span className="truncate text-xs text-fg-subtle">{providerLine(row)}</span>
      </span>
    ),
  }));

  return (
    <div className="flex flex-col gap-2">
      <Combobox
        id={id}
        items={items}
        value={value}
        onChange={(key) => {
          const row = models.models.find((candidate) => modelKey(candidate) === key);
          if (row?.available) onChange(row);
        }}
        placeholder={`Search ${models.models.length} allowlisted ${modality} models…`}
        emptyLabel="No allowlisted model matches"
        triggerLabel={<span className="text-fg-muted">Choose {modality === "image" ? "an image" : "a video"} model</span>}
      />
      {models.warning ? (
        <p className="text-xs text-fg-muted">
          OpenRouter&apos;s catalogue could not be reached, so these are its last known records: {models.warning}
        </p>
      ) : null}
    </div>
  );
}
