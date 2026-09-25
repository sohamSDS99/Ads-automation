/**
 * The Media Library's view model (Stage 04 PRD §15.4 G): the arithmetic of
 * drawing what the server stored — never of judging it. Which rendition is a
 * gap and why, what a lint verdict is, what a file's byte limit is and what a
 * frame scored are the server's, and pass through untouched.
 *
 * No `@/` imports: `pnpm test:unit` runs this file directly under node.
 */
import type {
  CaptionFrame,
  Concept,
  CreativeConceptsOutput,
  FittedLogo,
  ImageLintVerdict,
  ImageRenditionsOutput,
  Rendition,
  RenditionGap,
  ScriptCaption,
} from "../api/media-library";

// ---------------------------------------------------------------------------
// sizes and ratios
// ---------------------------------------------------------------------------

/** `1200x628` → `{width: 1200, height: 628}`; null for anything else. */
export function parsePx(px: string): { width: number; height: number } | null {
  const match = /^(\d+)x(\d+)$/.exec(px.trim());
  if (!match) return null;
  const width = Number(match[1]);
  const height = Number(match[2]);
  return width > 0 && height > 0 ? { width, height } : null;
}

/** `1.91:1` → 1.91, `9:16` → 0.5625; null for anything else. */
export function ratioValue(ratio: string): number | null {
  const match = /^(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)$/.exec(ratio.trim());
  if (!match) return null;
  const width = Number(match[1]);
  const height = Number(match[2]);
  return width > 0 && height > 0 ? width / height : null;
}

/**
 * The letterbox: a box of the file's true ratio, as large as fits inside a
 * frame of `frameRatio`, in percent of the frame. Never a crop — the grid does
 * not `object-cover` (§15.4 G).
 */
export function letterbox(ratio: number, frameRatio = 1): { width: number; height: number } {
  if (ratio >= frameRatio) return { width: 100, height: (frameRatio / ratio) * 100 };
  return { width: (ratio / frameRatio) * 100, height: 100 };
}

const KIB = 1024;
const MIB = 1024 * 1024;

/** Binary units, as the spec sheet's limits are written (5 MB = 5,242,880 bytes). */
export function bytesLabel(bytes: number): string {
  if (bytes < KIB) return `${bytes} B`;
  if (bytes < MIB) {
    const kib = bytes / KIB;
    return `${kib < 10 ? kib.toFixed(1) : Math.round(kib)} KB`;
  }
  const mib = bytes / MIB;
  return `${mib < 10 ? mib.toFixed(1).replace(/\.0$/, "") : Math.round(mib)} MB`;
}

/**
 * `184 KB / 5 MB`. `null` is a slot with no limit; `undefined` a run from
 * before the limit was recorded — said differently, because "no limit" would
 * be a claim the stored output does not make.
 */
export function bytesOfLimit(bytes: number, limit: number | null | undefined): string {
  if (limit === undefined) return `${bytesLabel(bytes)} · limit not recorded`;
  if (limit === null) return `${bytesLabel(bytes)} · no limit`;
  return `${bytesLabel(bytes)} / ${bytesLabel(limit)}`;
}

/** `1200 × 628`. */
export function pxLabel(px: string): string {
  const size = parsePx(px);
  return size ? `${size.width} × ${size.height}` : px;
}

// ---------------------------------------------------------------------------
// the rendition grid
// ---------------------------------------------------------------------------

export const SURFACE_LABEL: Record<string, string> = {
  search_image: "Search",
  pmax_image: "Performance Max",
  display_image: "Display",
  demand_gen_image: "Demand Gen",
};

export type Derivation = "native" | "relaid" | "crop" | "composited";

export const DERIVATION_LABEL: Record<Derivation, string> = {
  native: "Native",
  relaid: "Relaid",
  crop: "Saliency crop",
  composited: "Fitted logo",
};

/** The same, short enough for two chips on one row of a grid tile. */
export const DERIVATION_CHIP: Record<Derivation, string> = {
  native: "Native",
  relaid: "Relaid",
  crop: "Crop",
  composited: "Logo",
};

export type FileTile = {
  kind: "file";
  key: string;
  mediaId: string;
  assetId: string;
  campaignRef: string;
  conceptId: string | null;
  conceptName: string | null;
  surface: string;
  ratio: string;
  px: string;
  width: number;
  height: number;
  bytes: number;
  maxBytes: number | null | undefined;
  derivation: Derivation;
  lint: ImageLintVerdict;
  jobId: string | null;
  generated: boolean;
  rendition: Rendition | null;
  logo: FittedLogo | null;
};

export type GapTile = {
  kind: "gap";
  key: string;
  campaignRef: string;
  conceptId: string | null;
  conceptName: string | null;
  surface: string;
  ratio: string;
  /** The server's sentence, verbatim. */
  why: string;
};

export type Tile = FileTile | GapTile;
export type SurfaceGroup = { surface: string; label: string; tiles: Tile[]; files: number; gaps: number };

function conceptIndex(concepts: CreativeConceptsOutput | null): Map<string, { name: string; order: number }> {
  const index = new Map<string, { name: string; order: number }>();
  let order = 0;
  for (const campaign of concepts?.campaigns ?? []) {
    for (const concept of campaign.concepts) index.set(concept.id, { name: concept.name, order: order++ });
  }
  return index;
}

/**
 * Every rendition, fitted logo and recorded gap of 4.4.3, grouped by surface
 * in the order the spec sheet's surfaces are listed, then by campaign, then
 * by concept, wide ratios first — so a gap sits where its file would have.
 */
export function surfaceGroups(
  renditions: ImageRenditionsOutput | null,
  concepts: CreativeConceptsOutput | null,
): SurfaceGroup[] {
  if (!renditions) return [];
  const names = conceptIndex(concepts);
  const tiles: Tile[] = [];
  for (const item of renditions.renditions) {
    const size = parsePx(item.px);
    if (!size) continue;
    tiles.push({
      kind: "file",
      key: item.media_id,
      mediaId: item.media_id,
      assetId: item.asset_id,
      campaignRef: item.campaign_ref,
      conceptId: item.concept_id,
      conceptName: names.get(item.concept_id)?.name ?? null,
      surface: item.surface,
      ratio: item.ratio,
      px: item.px,
      ...size,
      bytes: item.bytes,
      maxBytes: item.max_bytes,
      derivation: item.derivation,
      lint: item.lint.verdict,
      jobId: item.job_id,
      generated: true,
      rendition: item,
      logo: null,
    });
  }
  for (const item of renditions.logos) {
    const size = parsePx(item.px);
    if (!size) continue;
    tiles.push({
      kind: "file",
      key: item.media_id,
      mediaId: item.media_id,
      assetId: item.asset_id,
      campaignRef: item.campaign_ref,
      conceptId: null,
      conceptName: null,
      surface: item.surface ?? "logo",
      ratio: item.ratio,
      px: item.px,
      ...size,
      bytes: item.bytes,
      maxBytes: item.max_bytes,
      derivation: "composited",
      lint: item.lint.verdict,
      jobId: null,
      generated: false,
      rendition: null,
      logo: item,
    });
  }
  renditions.gaps.forEach((gap: RenditionGap, i) => {
    tiles.push({
      kind: "gap",
      key: `gap:${gap.campaign_ref}:${gap.concept_id ?? "campaign"}:${gap.surface}:${gap.ratio}:${i}`,
      campaignRef: gap.campaign_ref,
      conceptId: gap.concept_id,
      conceptName: gap.concept_id ? (names.get(gap.concept_id)?.name ?? null) : null,
      surface: gap.surface,
      ratio: gap.ratio,
      why: gap.why,
    });
  });

  const order = (tile: Tile) => (tile.conceptId ? (names.get(tile.conceptId)?.order ?? 999) : 1000);
  const surfaces = [...Object.keys(SURFACE_LABEL), "logo"];
  const bySurface = new Map<string, Tile[]>();
  for (const tile of tiles) bySurface.set(tile.surface, [...(bySurface.get(tile.surface) ?? []), tile]);
  return [...bySurface.entries()]
    .sort(([a], [b]) => rank(surfaces, a) - rank(surfaces, b) || a.localeCompare(b))
    .map(([surface, list]) => {
      const sorted = [...list].sort(
        (a, b) =>
          a.campaignRef.localeCompare(b.campaignRef) ||
          order(a) - order(b) ||
          (ratioValue(b.ratio) ?? 0) - (ratioValue(a.ratio) ?? 0) ||
          a.key.localeCompare(b.key),
      );
      return {
        surface,
        label: SURFACE_LABEL[surface] ?? surface,
        tiles: sorted,
        files: sorted.filter((tile) => tile.kind === "file").length,
        gaps: sorted.filter((tile) => tile.kind === "gap").length,
      };
    });
}

function rank(order: string[], value: string): number {
  const at = order.indexOf(value);
  return at === -1 ? order.length : at;
}

export type GridRow =
  | { kind: "header"; key: string; group: SurfaceGroup }
  | { kind: "tiles"; key: string; tiles: Tile[] };

/** The groups as virtual rows: a header, then rows of `columns` tiles. */
export function gridRows(groups: SurfaceGroup[], columns: number): GridRow[] {
  const perRow = Math.max(1, Math.floor(columns));
  const rows: GridRow[] = [];
  for (const group of groups) {
    rows.push({ kind: "header", key: `h:${group.surface}`, group });
    for (let i = 0; i < group.tiles.length; i += perRow) {
      rows.push({ kind: "tiles", key: `r:${group.surface}:${i}`, tiles: group.tiles.slice(i, i + perRow) });
    }
  }
  return rows;
}

/** As many tiles of at least `minTile` px as fit `width`, `gap` px apart. */
export function columnsFor(width: number, minTile: number, gap: number): number {
  return Math.max(1, Math.floor((width + gap) / (minTile + gap)));
}

// ---------------------------------------------------------------------------
// the concept board
// ---------------------------------------------------------------------------

export type BoardConcept = Concept & { campaign_type: string };

export function boardConcepts(concepts: CreativeConceptsOutput | null): BoardConcept[] {
  return (concepts?.campaigns ?? []).flatMap((campaign) =>
    campaign.concepts.map((concept) => ({ ...concept, campaign_type: campaign.campaign_type })),
  );
}

/**
 * Which brief line an angle key names (4.4.1 `angle`): `angle` is the brief's
 * own angle; `{ad_group}:primary` and `{ad_group}:angle_b` an ad group's
 * primary message and variant-B angle.
 */
export function angleSource(key: string): { adGroupRef: string | null; line: "angle" | "primary_message" | "angle_b" } {
  if (key === "angle") return { adGroupRef: null, line: "angle" };
  const at = key.lastIndexOf(":");
  const part = key.slice(at + 1);
  return {
    adGroupRef: at === -1 ? null : key.slice(0, at),
    line: part === "angle_b" ? "angle_b" : "primary_message",
  };
}

const HEX = /^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i;

/**
 * A palette token's colour: the brand's own `{name, hex}` pairs, at the
 * guideline the run pinned; a token that is itself a hex (4.1.1 falls back to
 * one when a colour has no name) is its own value. Null when unknown — then
 * the swatch says so rather than invent a colour.
 */
export function tokenColour(token: string, palette: { name?: unknown; hex?: unknown }[]): string | null {
  const found = palette.find((entry) => entry.name === token || entry.hex === token);
  const hex = typeof found?.hex === "string" ? found.hex : token;
  return HEX.test(hex) ? hex : null;
}

// ---------------------------------------------------------------------------
// the video timeline
// ---------------------------------------------------------------------------

/** A time as a share of the video, clamped to the track, in percent. */
export function atPercent(ms: number, durationMs: number): number {
  if (durationMs <= 0) return 0;
  return Math.min(100, Math.max(0, (ms / durationMs) * 100));
}

export type CaptionMarker = {
  index: number;
  startMs: number;
  endMs: number;
  text: string;
  /** What OCR read at this caption, and how close — null if it was not sampled. */
  ocr: CaptionFrame | null;
};

/** The script's caption intervals, each beside the OCR sample verify.py took of it. */
export function captionMarkers(captions: ScriptCaption[], frames: CaptionFrame[]): CaptionMarker[] {
  return captions.map((caption, index) => ({
    index,
    startMs: Math.round(caption.t0 * 1000),
    endMs: Math.round(caption.t1 * 1000),
    text: caption.text,
    ocr: frames.find((frame) => frame.index === index) ?? null,
  }));
}

/** The end card: the last `endCardMs` of the file. Null when not recorded. */
export function endCardRegion(
  durationMs: number,
  endCardMs: number | null | undefined,
): { startMs: number; endMs: number } | null {
  if (endCardMs === null || endCardMs === undefined || endCardMs <= 0) return null;
  return { startMs: Math.max(0, durationMs - endCardMs), endMs: durationMs };
}

/** `4.2 s`, `12 s`. */
export function secondsLabel(ms: number): string {
  const s = ms / 1000;
  return `${Number.isInteger(s) ? s : s.toFixed(1)} s`;
}
