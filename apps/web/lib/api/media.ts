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

/* -------------------------------------------------------------------------
 * The Start dialog's model list (S4-P3)
 * ---------------------------------------------------------------------- */

/** `calc.media` / `media.capability.ratio_coverage`: how one required ratio gets made. */
export type RatioPlan = "native" | "relaid" | "crop" | "gap";

/** `schemas_media.MediaModelRow`: one allowlisted model, as the live catalogue describes it. */
export type MediaModelRow = {
  modality: MediaModality;
  model_id: string;
  provider_tag: string | null;
  enabled: boolean;
  /** The server's answer: listed but not choosable when false, with `reason`. */
  available: boolean;
  reason: "media_model_unavailable" | "disabled" | null;
  capability: CapabilityRecord | null;
  capability_hash: string | null;
  /** Against the project's spec sheet; `null` when the project has none to check. */
  ratio_coverage: Record<string, RatioPlan> | null;
};

/** `GET /media/models` — the allowlist ∩ the live catalogue. */
export type MediaModelsResponse = {
  models: MediaModelRow[];
  catalogue_hash: string | null;
  warning: string | null;
};

export function getMediaModels(modality: MediaModality, projectId: string) {
  return apiFetch<MediaModelsResponse>(
    `/media/models?modality=${modality}&project_id=${encodeURIComponent(projectId)}`,
  );
}

/** Why a listed model cannot be chosen, in a person's words. */
export const UNAVAILABLE_REASON: Record<NonNullable<MediaModelRow["reason"]>, string> = {
  media_model_unavailable: "Not in OpenRouter's live catalogue any more",
  disabled: "Switched off by an admin",
};

/**
 * One run default the Start dialog can set, as the control that sets it.
 *
 * `numeric` marks an enum whose values the request carries as numbers (a
 * video's `duration`); everything else in an enum is sent as the string the
 * catalogue lists.
 */
export type RunParam =
  | { field: string; kind: "enum"; values: string[]; numeric: boolean }
  | { field: string; kind: "range"; min: number | null; max: number | null }
  | { field: string; kind: "boolean" };

/**
 * The run defaults the dialog offers, in the order it shows them — a subset
 * of the server's default fields (`schemas_media.*_DEFAULT_FIELDS`), which is
 * what validates them. Three are left out on purpose, each for the same
 * reason the ratio-coverage table exists: `aspect_ratio` and `size` fix the
 * frame's shape, which the ratio plan decides per rendition, and `seed` is
 * catalogued as a may-send flag for an integer that a run-wide value would
 * repeat across every candidate (docs/stage-04-questions.md, S4-P3).
 */
const RUN_FIELDS: Record<MediaModality, string[]> = {
  image: ["resolution", "quality", "output_format", "background", "output_compression", "n"],
  video: ["duration", "resolution", "generate_audio"],
};

/**
 * The controls `CapabilityParams` draws for one model: **only what its
 * capability record says it supports**. A parameter with no descriptor — or
 * an empty `supported_*` list — is absent, never disabled. A parameter with a
 * single possible value offers no choice and is not drawn either.
 *
 * Where each field's descriptor lives is the one fact this mirrors from
 * `media.capability.validate`: an image's are `params[field]`; a video's
 * duration and resolution are its `supported_*` lists, its flags are
 * `params`. The server still validates every value it is sent.
 */
export function runParams(record: CapabilityRecord): RunParam[] {
  const params: RunParam[] = [];
  for (const field of RUN_FIELDS[record.modality]) {
    if (record.modality === "video" && (field === "duration" || field === "resolution")) {
      const listed =
        field === "duration"
          ? [...(record.video?.durations ?? [])].sort((a, b) => a - b).map(String)
          : (record.video?.resolutions ?? []);
      if (listed.length > 1) {
        params.push({ field, kind: "enum", values: listed, numeric: field === "duration" });
      }
      continue;
    }
    const descriptor = record.params[field];
    if (!descriptor) continue;
    if (descriptor.kind === "enum") {
      const values = descriptor.values ?? [];
      if (values.length > 1) params.push({ field, kind: "enum", values, numeric: false });
    } else if (descriptor.kind === "range") {
      const min = descriptor.min ?? null;
      const max = descriptor.max ?? null;
      if (min === null || max === null || max > min) params.push({ field, kind: "range", min, max });
    } else {
      params.push({ field, kind: "boolean" });
    }
  }
  return params;
}

/** A parameter's name as a label: `output_compression` → `Output compression`. */
export const PARAM_LABEL: Record<string, string> = {
  resolution: "Resolution",
  quality: "Quality",
  output_format: "Format",
  background: "Background",
  output_compression: "Compression",
  n: "Images per request",
  duration: "Duration",
  generate_audio: "Generate audio",
};

/** `"1.91:1"` → 1.91. `null` for anything that is not a ratio (`auto`). */
export function ratioValue(label: string): number | null {
  const [w, h] = label.split(":").map(Number);
  if (w === undefined || h === undefined || !Number.isFinite(w) || !Number.isFinite(h) || h <= 0) {
    return null;
  }
  return w / h;
}
