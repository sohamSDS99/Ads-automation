/**
 * Stage 04's media settings: which image and video models may be chosen.
 *
 * The routes are PRD §16's (`GET /media/catalogue`, `GET` and `PUT
 * /settings/media`); the shapes are the ones §7 and the S4-P1 brief name —
 * `CapabilityRecord{modality, model_id, provider_tag?, params, video,
 * pricing, input_modalities}` and `media_allowlist {image: [{model_id,
 * provider_tag?, enabled}], video: [...]}`. S4-P1 builds the API in parallel
 * with this file, so the two envelopes the PRD leaves implicit — the
 * catalogue's `{modality, models, catalogue_hash, warning}` and the settings'
 * `{media_allowlist}` — are recorded in docs/stage-04-questions.md for that
 * phase to confirm or correct.
 *
 * Nothing is allowlisted by default (§9.2): an empty list is the starting
 * state, not an error, and it is what keeps a media run from starting.
 */
import { apiFetch } from "@/lib/api";

export type MediaModality = "image" | "video";

/** One request parameter a model accepts (§9.1 rule 2). An absent key means unsupported. */
export type ParamDescriptor = {
  kind: "enum" | "range" | "boolean";
  values?: (string | number)[];
  min?: number;
  max?: number;
};

export type VideoCapability = {
  durations: number[];
  resolutions: string[];
  aspect_ratios: string[];
  sizes: string[];
};

/** A price line: `unit` is `image`, `megapixel` or `token` for images, `second` for video. */
export type PriceLine = { unit: string; variant?: string | null; usd: string | number };

export type CapabilityRecord = {
  modality: MediaModality;
  model_id: string;
  /** Set when the record describes one provider's endpoint of the model. */
  provider_tag?: string | null;
  params: Record<string, ParamDescriptor>;
  video?: VideoCapability | null;
  pricing: PriceLine[];
  input_modalities: string[];
};

/** `GET /media/catalogue?modality=` — the full live catalogue, for the allowlist editor. */
export type MediaCatalogue = {
  modality: MediaModality;
  models: CapabilityRecord[];
  catalogue_hash: string;
  /** Present when a last-good snapshot is being served because the live fetch failed (§9.1 rule 1). */
  warning?: string | null;
};

export type MediaAllowlistEntry = {
  model_id: string;
  /** A pinned provider; `null` lets OpenRouter route within the model (§9.1 rule 6). */
  provider_tag?: string | null;
  enabled: boolean;
};

export type MediaAllowlist = Record<MediaModality, MediaAllowlistEntry[]>;

/** `GET /settings/media`, and the body `PUT /settings/media` takes back. */
export type MediaSettings = { media_allowlist: MediaAllowlist };

export function getMediaCatalogue(modality: MediaModality) {
  return apiFetch<MediaCatalogue>(`/media/catalogue?modality=${modality}`);
}

export function getMediaSettings() {
  return apiFetch<MediaSettings>("/settings/media");
}

export function putMediaSettings(settings: MediaSettings) {
  return apiFetch<MediaSettings>("/settings/media", {
    method: "PUT",
    body: JSON.stringify(settings),
  });
}

/** The allowlist a workspace starts with. */
export const EMPTY_ALLOWLIST: MediaAllowlist = { image: [], video: [] };

/* -------------------------------------------------------------------------
 * How a capability record reads in a table row
 * ---------------------------------------------------------------------- */

/** The short facts a row shows beside the model (`1K 2K 4K`, `4–8 s`, `audio`, `image input`). */
export function capabilityChips(record: CapabilityRecord): string[] {
  const chips: string[] = [];
  const resolution = record.params.resolution;
  if (record.modality === "image" && resolution?.kind === "enum" && resolution.values?.length) {
    chips.push(resolution.values.join(" "));
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
  token: "token",
  second: "s",
};

function money(value: string | number): string {
  const amount = typeof value === "number" ? value : Number(value);
  if (Number.isNaN(amount)) return String(value);
  // Media prices live in cents and fractions of cents; two places would round
  // a $0.004 token price to "$0.00", which is a claim that it is free.
  const places = amount !== 0 && Math.abs(amount) < 0.01 ? 4 : 2;
  return `$${amount.toFixed(places)}`;
}

/** `$0.04 / image at 1K`, `$0.10 / s at 720p`. */
export function priceLabel(line: PriceLine): string {
  const unit = UNIT[line.unit] ?? line.unit;
  return `${money(line.usd)} / ${unit}${line.variant ? ` at ${line.variant}` : ""}`;
}
