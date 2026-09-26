/**
 * Display helpers for the QA and Package screens (Stage 04 PRD §15.4 K, L).
 *
 * **Presentation only.** Every verdict these screens show is the server's —
 * 4.6.4's preview verdict and truncation facts, 4.6.1's conformance, 4.7.2's
 * thirteen checks, whether a package is releasable and which version release
 * mints. What is here groups those facts for display, names them in words, and
 * lays out a manifest as a tree. Pure, so `pnpm test:unit` covers it.
 */
import type {
  ManifestEntry,
  PreviewBox,
  PreviewDom,
  RenderPreviewItem,
} from "../api/creative-packages.ts";

// ---------------------------------------------------------------------------
// previews
// ---------------------------------------------------------------------------

const ELEMENT = /^(headline|description|path)_(\d+)$/;

/** `headline_2` → "Headline 2"; `display_url` → "Display URL". */
export function elementLabel(key: string): string {
  if (key === "display_url") return "Display URL";
  const found = ELEMENT.exec(key);
  if (!found) return key.replace(/_/g, " ");
  const [, kind, index] = found;
  return `${kind!.charAt(0).toUpperCase()}${kind!.slice(1)} ${index}`;
}

/** `["likely_2", "longest"]` → "Likely 2 · longest": the order 4.6.4 chose it in. */
export function rolesLabel(roles: readonly string[] | undefined): string {
  if (!roles || roles.length === 0) return "Combination";
  return roles
    .map((role, index) => {
      const likely = /^likely_(\d+)$/.exec(role);
      const word = likely ? `likely ${likely[1]}` : role.replace(/_/g, " ");
      return index === 0 ? `${word.charAt(0).toUpperCase()}${word.slice(1)}` : word;
    })
    .join(" · ");
}

/** An RSA's ref, `campaign/ad group/variant`, as its three parts. */
export function adRefParts(ref: string): { campaign: string; adGroup: string; variant: string } {
  const parts = ref.split("/");
  if (parts.length < 3) return { campaign: ref, adGroup: "", variant: "" };
  const variant = parts[parts.length - 1]!;
  return { campaign: parts[0]!, adGroup: parts.slice(1, -1).join("/"), variant };
}

export type PreviewPair = {
  /** The combination's roles, joined — one per combination of one RSA. */
  key: string;
  roles: string[];
  likelihood: number | null;
  mobile: RenderPreviewItem | null;
  desktop: RenderPreviewItem | null;
};

export type PreviewGroup = { adRef: string; pairs: PreviewPair[] };

/**
 * The server's list (ordered by RSA, then 4.6.4's order of choice, then
 * device) as one group per RSA and one phone/desktop pair per combination.
 * The server's order is kept; nothing is re-ranked.
 */
export function groupPreviews(items: readonly RenderPreviewItem[]): PreviewGroup[] {
  const groups: PreviewGroup[] = [];
  const byRef = new Map<string, PreviewGroup>();
  for (const item of items) {
    let group = byRef.get(item.ad_ref);
    if (!group) {
      group = { adRef: item.ad_ref, pairs: [] };
      byRef.set(item.ad_ref, group);
      groups.push(group);
    }
    const roles = item.combination.roles ?? [];
    const key = roles.join("+") || item.id;
    let pair = group.pairs.find((candidate) => candidate.key === key);
    if (!pair) {
      pair = {
        key,
        roles: [...roles],
        likelihood: typeof item.combination.likelihood === "number" ? item.combination.likelihood : null,
        mobile: null,
        desktop: null,
      };
      group.pairs.push(pair);
    }
    pair[item.device] = item;
  }
  return groups;
}

export type Truncation = {
  key: string;
  label: string;
  /** How far the text runs past its slot, in CSS px — 0 when only clipped. */
  overflowPx: number;
  /** The device layout hides it: past the block's line clamp or its edge. */
  clipped: boolean;
  /** Where to mark it on the capture; null on a preview measured before boxes. */
  box: PreviewBox | null;
  /** The block that clips it, when 4.6.4 recorded one. */
  clip: PreviewBox | null;
};

/** What 4.6.4 measured as truncated, in its order: overflowing its slot, or clipped. */
export function truncations(dom: PreviewDom): Truncation[] {
  const elements = dom.elements ?? [];
  if (elements.length > 0) {
    return elements
      .filter((m) => m.overflow_px > 0 || m.clipped)
      .map((m) => ({
        key: m.key,
        label: elementLabel(m.key),
        overflowPx: m.overflow_px,
        clipped: m.clipped,
        box: m.box ?? null,
        clip: m.clip_box ?? null,
      }));
  }
  const px = new Map((dom.overflow_px ?? []).map((o) => [o.element, o.px]));
  return (dom.truncated ?? []).map((key) => ({
    key,
    label: elementLabel(key),
    overflowPx: px.get(key) ?? 0,
    clipped: !px.has(key),
    box: null,
    clip: null,
  }));
}

/** "+14 px past its slot" or "clipped by the layout" — the words beside a mark. */
export function truncationText(t: Pick<Truncation, "overflowPx" | "clipped">): string {
  if (t.overflowPx > 0 && t.clipped) return `${t.overflowPx} px past its slot and clipped`;
  if (t.overflowPx > 0) return `${t.overflowPx} px past its slot`;
  return "clipped by the layout";
}

function intersect(a: PreviewBox, b: PreviewBox): PreviewBox | null {
  const x = Math.max(a.x, b.x);
  const y = Math.max(a.y, b.y);
  const right = Math.min(a.x + a.width, b.x + b.width);
  const bottom = Math.min(a.y + a.height, b.y + b.height);
  return right - x >= 2 && bottom - y >= 2 ? { x, y, width: right - x, height: bottom - y } : null;
}

export type Mark = { kind: "box" | "edge"; rect: PreviewBox };

/**
 * Where to draw a truncation, in the cropped frame's coordinates.
 *
 * An element that overflows its slot is outlined where it is. A clipped one
 * is outlined only where it still shows — its box is the space the hidden
 * text would take, which lies under the next block — and when nothing of it
 * shows, the mark is the edge of the block that cut it off. Null when the
 * mark would fall outside the frame, or 4.6.4 recorded no box.
 */
export function markFor(t: Pick<Truncation, "box" | "clip" | "clipped" | "overflowPx">, frame: PreviewBox | null): Mark | null {
  if (!t.box) return null;
  let mark: Mark = { kind: "box", rect: t.box };
  if (t.clipped && t.clip) {
    const shown = intersect(t.box, t.clip);
    mark = shown
      ? { kind: "box", rect: shown }
      : { kind: "edge", rect: { x: t.clip.x, y: t.clip.y + t.clip.height - 1, width: t.clip.width, height: 2 } };
  }
  const at = markIn(mark.rect, frame);
  return at ? { kind: mark.kind, rect: at } : null;
}

/** A mark's box in the cropped frame's coordinates, or null when it lies outside it. */
export function markIn(box: PreviewBox, frame: PreviewBox | null): PreviewBox | null {
  if (!frame) return box;
  const x = box.x - frame.x;
  const y = box.y - frame.y;
  if (x + box.width <= 0 || y + box.height <= 0 || x >= frame.width || y >= frame.height) return null;
  return { x, y, width: box.width, height: box.height };
}

// ---------------------------------------------------------------------------
// ids, hashes, sizes
// ---------------------------------------------------------------------------

/** `a41f…9c2e` — §15.2 rule 5: both ends, which are what a person matches by eye. */
export function middle(value: string, head = 4, tail = 4): string {
  return value.length > head + tail + 1 ? `${value.slice(0, head)}…${value.slice(-tail)}` : value;
}

/** Bytes in binary units, the way a file manager lists them: `812 B`, `12.4 KiB`. */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KiB", "MiB", "GiB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 100 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
}

// ---------------------------------------------------------------------------
// the manifest as a tree
// ---------------------------------------------------------------------------

export type ManifestFile = { kind: "file"; name: string; entry: ManifestEntry };
export type ManifestDir = { kind: "dir"; name: string; path: string; bytes: number; files: number; children: ManifestNode[] };
export type ManifestNode = ManifestFile | ManifestDir;

/**
 * The manifest's paths as folders and files: folders first, each level by
 * name, a folder carrying the bytes and file count of everything under it.
 */
export function manifestTree(entries: readonly ManifestEntry[]): ManifestDir {
  const root: ManifestDir = { kind: "dir", name: "", path: "", bytes: 0, files: 0, children: [] };
  for (const entry of entries) {
    const parts = entry.path.split("/").filter(Boolean);
    let dir = root;
    root.bytes += entry.bytes;
    root.files += 1;
    for (const [index, part] of parts.slice(0, -1).entries()) {
      let next = dir.children.find((child): child is ManifestDir => child.kind === "dir" && child.name === part);
      if (!next) {
        next = { kind: "dir", name: part, path: parts.slice(0, index + 1).join("/"), bytes: 0, files: 0, children: [] };
        dir.children.push(next);
      }
      next.bytes += entry.bytes;
      next.files += 1;
      dir = next;
    }
    dir.children.push({ kind: "file", name: parts[parts.length - 1] ?? entry.path, entry });
  }
  const sort = (dir: ManifestDir) => {
    dir.children.sort((a, b) => (a.kind === b.kind ? a.name.localeCompare(b.name) : a.kind === "dir" ? -1 : 1));
    for (const child of dir.children) if (child.kind === "dir") sort(child);
  };
  sort(root);
  return root;
}

// ---------------------------------------------------------------------------
// release
// ---------------------------------------------------------------------------

/**
 * Whether what the approver typed is the version to mint, exactly: `v3`. An
 * input check, not a verdict — the server compares `confirm_version` with the
 * version it mints and refuses a mismatch (409 `version_mismatch`).
 */
export function confirmsVersion(typed: string, version: number): boolean {
  return typed.trim() === `v${version}`;
}

/** Actual against estimate, both the server's: `+$0.40 (+12%)`. Null with no estimate. */
export function costDelta(estimate: string, actual: string): { usd: number; pct: number | null } {
  const e = Number(estimate);
  const a = Number(actual);
  return { usd: a - e, pct: e > 0 ? ((a - e) / e) * 100 : null };
}

// ---------------------------------------------------------------------------
// the diff's fields
// ---------------------------------------------------------------------------

export type FieldChangeRow = { field: string; before: string; after: string };

function plain(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** A field value in words: `—` for nothing, JSON for a list or an object. */
export function fieldText(value: unknown): string {
  if (value === null || value === undefined) return "—";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

/**
 * The fields `package_diff` reported on a changed asset, as rows of what
 * moved: a field whose value is an object (an offer binding) is opened one
 * level, so the row reads `bound · percent_off 23 → 31`, not two JSON blobs.
 * Which asset changed is the server's; this only lays out its two sides.
 */
export function changedFields(before: Record<string, unknown>, after: Record<string, unknown>): FieldChangeRow[] {
  const rows: FieldChangeRow[] = [];
  for (const key of [...new Set([...Object.keys(before), ...Object.keys(after)])]) {
    const a = before[key];
    const b = after[key];
    if (plain(a) && plain(b)) {
      for (const sub of [...new Set([...Object.keys(a), ...Object.keys(b)])]) {
        if (fieldText(a[sub]) !== fieldText(b[sub])) {
          rows.push({ field: `${key.replace(/_/g, " ")} · ${sub.replace(/_/g, " ")}`, before: fieldText(a[sub]), after: fieldText(b[sub]) });
        }
      }
    } else if (fieldText(a) !== fieldText(b)) {
      rows.push({ field: key.replace(/_/g, " "), before: fieldText(a), after: fieldText(b) });
    }
  }
  return rows;
}
