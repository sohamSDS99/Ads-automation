/**
 * One zoom, shared by every view of a G8 item (Stage 04 PRD §15.4 H:
 * `ReferenceCompare` — "product reference and generated asset with
 * synchronised zoom").
 *
 * The centre is a fraction of the *image*, not of its frame: each view draws
 * its file in a box of the file's true ratio (letterboxed, never cropped), so
 * `{scale: 2, cx: 0.3, cy: 0.6}` shows the same region of a 1:1 reference and
 * a 16:9 rendition. The centre is clamped so a zoomed view never shows past
 * the file's edge.
 */
export type Zoom = { scale: number; cx: number; cy: number };

export const FIT: Zoom = { scale: 1, cx: 0.5, cy: 0.5 };

/** The zoom steps `+` and `-` walk. 1 is fit. */
export const STEPS: readonly number[] = [1, 1.5, 2, 3, 4, 6];

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}

export function clampZoom(zoom: Zoom): Zoom {
  const scale = clamp(zoom.scale, STEPS[0] ?? 1, STEPS[STEPS.length - 1] ?? 1);
  const half = 0.5 / scale;
  return { scale, cx: clamp(zoom.cx, half, 1 - half), cy: clamp(zoom.cy, half, 1 - half) };
}

export function zoomIn(zoom: Zoom): Zoom {
  const next = STEPS.find((step) => step > zoom.scale + 1e-9);
  return clampZoom({ ...zoom, scale: next ?? zoom.scale });
}

export function zoomOut(zoom: Zoom): Zoom {
  const lower = STEPS.filter((step) => step < zoom.scale - 1e-9);
  return clampZoom({ ...zoom, scale: lower[lower.length - 1] ?? zoom.scale });
}

/** Move the centre by a fraction of what is on screen (0.1 = a tenth of the view). */
export function pan(zoom: Zoom, dx: number, dy: number): Zoom {
  return clampZoom({ ...zoom, cx: zoom.cx + dx / zoom.scale, cy: zoom.cy + dy / zoom.scale });
}

/**
 * The CSS transform (origin `0 0`) that puts the image point `(cx, cy)` at the
 * centre of its box at `scale`: `s·c·W + t = W/2`, with `translate()`
 * percentages of the box's own size.
 */
export function transformOf(zoom: Zoom): string {
  const x = (0.5 - zoom.scale * zoom.cx) * 100;
  const y = (0.5 - zoom.scale * zoom.cy) * 100;
  return `translate(${x.toFixed(3)}%, ${y.toFixed(3)}%) scale(${zoom.scale})`;
}

export function percent(zoom: Zoom): string {
  return `${Math.round(zoom.scale * 100)}%`;
}
