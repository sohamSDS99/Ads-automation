/**
 * Stage 04's media settings: which image and video models may be chosen.
 *
 * Mirrors `agent.api.schemas_media` and `agent.media.types` (S4-P1): `GET
 * /media/catalogue` (SETTINGS_WRITE) returns the live catalogue's
 * `CapabilityRecord`s, and `GET`/`PUT /settings/media` read and write the
 * workspace's `media_allowlist` and `media_defaults`.
 *
 * Two facts about the real catalogue shape what the editor can show:
 *
 * - An image record is OpenRouter's `/images/models` summary — the model's
 *   parameter *union*, with **no pricing and no provider tag**. Both live on
 *   the per-provider endpoints, which no route exposes to the editor yet, so
 *   the editor cannot offer a provider pin (docs/stage-04-questions.md, S4-P2).
 * - A video model can never pin a provider: its request has no routing field,
 *   and `PUT /settings/media` refuses a video `provider_tag`.
 *
 * Nothing is allowlisted by default (§9.2): an empty list is the starting
 * state, not an error, and it is what keeps a media run from starting.
 */
import { apiFetch } from "@/lib/api";

export type MediaModality = "image" | "video";

/** `media.types.Descriptor`: one request parameter a model accepts. Absent means unsupported. */
export type ParamDescriptor = {
  kind: "enum" | "range" | "boolean";
  values?: string[] | null;
  min?: number | null;
  max?: number | null;
};

/** `media.types.VideoCaps`. Empty means unsupported. */
export type VideoCapability = {
  durations: number[];
  resolutions: string[];
  aspect_ratios: string[];
  sizes: string[];
  frame_images: string[];
};

/**
 * `media.types.PriceLine`. Images carry the endpoint's lines verbatim (`unit`
 * image | megapixel | token | request); video carries one per SKU (`unit`
 * `second`, `variant` its resolution, `audio` and `mode` when the SKU names
 * them). `unknown` is kept rather than dropped, so it never reads as free.
 */
export type PriceLine = {
  billable: string;
  unit: string;
  /** A decimal, serialised as a string. */
  usd: string;
  variant?: string | null;
  audio?: boolean | null;
  mode?: "text_to_video" | "image_to_video" | null;
  sku?: string | null;
};

export type CapabilityRecord = {
  modality: MediaModality;
  model_id: string;
  provider_tag?: string | null;
  params: Record<string, ParamDescriptor>;
  video?: VideoCapability | null;
  pricing: PriceLine[];
  input_modalities: string[];
};

/** `schemas_media.CatalogueResponse`. `warning` is set when a last-good snapshot is served. */
export type MediaCatalogue = {
  modality: MediaModality;
  models: CapabilityRecord[];
  catalogue_hash: string;
  fetched_at: string;
  warning?: string | null;
};

/** `schemas_media.AllowlistEntry`. */
export type MediaAllowlistEntry = {
  model_id: string;
  provider_tag?: string | null;
  enabled: boolean;
};

export type MediaAllowlist = Record<MediaModality, MediaAllowlistEntry[]>;

/** `schemas_media.MediaDefaults`: workspace default params per modality. */
export type MediaDefaults = Record<MediaModality, Record<string, unknown>>;

/** `GET /settings/media`. */
export type MediaSettings = { media_allowlist: MediaAllowlist; media_defaults: MediaDefaults };

/** `PUT /settings/media`. A section left out is left as it is. */
export type MediaSettingsUpdate = { media_allowlist?: MediaAllowlist; media_defaults?: MediaDefaults };

export function getMediaCatalogue(modality: MediaModality) {
  return apiFetch<MediaCatalogue>(`/media/catalogue?modality=${modality}`);
}

export function getMediaSettings() {
  return apiFetch<MediaSettings>("/settings/media");
}

export function putMediaSettings(update: MediaSettingsUpdate) {
  return apiFetch<MediaSettings>("/settings/media", {
    method: "PUT",
    body: JSON.stringify(update),
  });
}

/** The allowlist a workspace starts with. */
export const EMPTY_ALLOWLIST: MediaAllowlist = { image: [], video: [] };

/* -------------------------------------------------------------------------
 * How a capability record reads in a table row
 * ---------------------------------------------------------------------- */

/** The short facts a row shows beside the model (`10 ratios`, `4–8 s`, `720p 1080p`, `audio`, `image input`). */
export function capabilityChips(record: CapabilityRecord): string[] {
  const chips: string[] = [];
  const resolution = record.params.resolution;
  if (resolution?.kind === "enum" && resolution.values?.length) chips.push(resolution.values.join(" "));
  const ratios = record.params.aspect_ratio;
  if (record.modality === "image" && ratios?.kind === "enum" && ratios.values?.length) {
    chips.push(ratios.values.length === 1 ? "1 ratio" : `${ratios.values.length} ratios`);
  }
  if (record.modality === "video" && record.video) {
    const durations = [...record.video.durations].sort((a, b) => a - b);
    const first = durations[0];
    const last = durations[durations.length - 1];
    if (first !== undefined && last !== undefined) {
      chips.push(first === last ? `${first} s` : `${first}–${last} s`);
    }
    if (record.video.resolutions.length > 0) chips.push(record.video.resolutions.join(" "));
    if (record.params.generate_audio) chips.push("audio");
  }
  if (record.input_modalities.includes("image")) chips.push("image input");
  return chips;
}

const UNIT: Record<string, string> = {
  image: "image",
  megapixel: "megapixel",
  request: "request",
  second: "s",
};

function money(amount: number): string {
  // Media prices live in cents and fractions of cents. Fixed places would
  // round $0.0004 to "$0.00" — a claim that it is free — so anything under a
  // cent keeps its first two significant figures instead.
  if (amount !== 0 && Math.abs(amount) < 0.01) {
    return `$${amount.toLocaleString("en-US", { maximumSignificantDigits: 2, useGrouping: false })}`;
  }
  return `$${amount.toFixed(2)}`;
}

/**
 * `$0.05 / s at 720p with audio`, `$0.04 / image at 1K`, `$40.00 / 1M tokens`.
 *
 * A per-token price is quoted per million, as the text-model table quotes
 * it. A line the catalogue parser could not read (`unknown`) is shown as the
 * SKU it came from, never dropped.
 */
export function priceLabel(line: PriceLine): string {
  const amount = Number(line.usd);
  const qualifiers = [
    line.variant ? `at ${line.variant}` : null,
    line.audio === true ? "with audio" : line.audio === false ? "without audio" : null,
    line.mode === "image_to_video" ? "from an image" : null,
  ].filter(Boolean);
  const tail = qualifiers.length > 0 ? ` ${qualifiers.join(" ")}` : "";
  if (Number.isNaN(amount) || line.unit === "unknown") {
    return `${line.usd} per ${line.sku ?? line.billable}`;
  }
  if (line.unit === "token") return `${money(amount * 1_000_000)} / 1M tokens${tail}`;
  return `${money(amount)} / ${UNIT[line.unit] ?? line.unit}${tail}`;
}

/** The cheapest line first: what a row leads with, the rest in its tooltip. */
export function sortedPrices(record: CapabilityRecord): PriceLine[] {
  return [...record.pricing].sort((a, b) => Number(a.usd) - Number(b.usd));
}
