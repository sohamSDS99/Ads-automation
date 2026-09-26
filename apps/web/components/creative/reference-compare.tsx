"use client";

import { Maximize, Minus, Plus } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";

import { CHECKER_ID } from "@/components/creative/media-frame";
import { Button } from "@/components/ui/button";
import { letterbox } from "@/lib/creative/media-library";
import { FIT, pan, percent, transformOf, zoomIn, zoomOut, type Zoom } from "@/lib/creative/zoom";
import { cn } from "@/lib/utils";

/** The size of an element, kept current. */
function useBoxSize<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const observer = new ResizeObserver(([entry]) => {
      if (!entry) return;
      const { width, height } = entry.contentRect;
      setSize((current) =>
        Math.abs(current.width - width) < 0.5 && Math.abs(current.height - height) < 0.5
          ? current
          : { width, height },
      );
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  return [ref, size] as const;
}

/**
 * One file at its TRUE ratio, letterboxed on the checkerboard inside whatever
 * frame it is given — never cropped to fill (§15.4 G, H) — at the shared zoom.
 * Dragging pans every view that shares the zoom; so do the arrow keys when
 * this view has focus.
 */
export function ZoomedImage({
  src,
  ratio,
  alt,
  zoom,
  onZoom,
  className,
}: {
  src: string;
  /** width / height of the file. */
  ratio: number;
  alt: string;
  zoom: Zoom;
  onZoom: (next: Zoom) => void;
  className?: string;
}) {
  const [frame, size] = useBoxSize<HTMLDivElement>();
  const drag = useRef<{ x: number; y: number; zoom: Zoom } | null>(null);
  const box = size.width > 0 && size.height > 0 ? letterbox(ratio, size.width / size.height) : null;
  const boxWidth = box ? (box.width / 100) * size.width : 0;
  const boxHeight = box ? (box.height / 100) * size.height : 0;

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (zoom.scale <= 1) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { x: event.clientX, y: event.clientY, zoom };
  };
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const start = drag.current;
    if (!start || boxWidth === 0) return;
    onZoom(pan(start.zoom, -(event.clientX - start.x) / boxWidth, -(event.clientY - start.y) / boxHeight));
  };
  const onPointerUp = () => {
    drag.current = null;
  };
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (zoom.scale <= 1) return;
    const step = event.shiftKey ? 0.5 : 0.1;
    const moves: Record<string, [number, number]> = {
      ArrowLeft: [-step, 0],
      ArrowRight: [step, 0],
      ArrowUp: [0, -step],
      ArrowDown: [0, step],
    };
    const move = moves[event.key];
    if (!move) return;
    event.preventDefault();
    event.stopPropagation();
    onZoom(pan(zoom, move[0], move[1]));
  };

  return (
    <div ref={frame} className={cn("relative grid size-full place-items-center overflow-hidden", className)}>
      <svg aria-hidden className="absolute inset-0 size-full">
        <rect width="100%" height="100%" fill={`url(#${CHECKER_ID})`} />
      </svg>
      <div
        data-letterbox=""
        // Focusable only when there is something to pan.
        tabIndex={zoom.scale > 1 ? 0 : -1}
        aria-label={zoom.scale > 1 ? `${alt}, zoomed to ${percent(zoom)}. Arrow keys pan.` : undefined}
        role={zoom.scale > 1 ? "group" : undefined}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onKeyDown={onKeyDown}
        className={cn("relative overflow-hidden", zoom.scale > 1 && "cursor-grab touch-none active:cursor-grabbing")}
        style={{ width: boxWidth, height: boxHeight }}
      >
        {box ? (
          // eslint-disable-next-line @next/next/no-img-element -- a signed 302 the optimiser cannot follow
          <img
            src={src}
            alt={alt}
            decoding="async"
            draggable={false}
            className="absolute inset-0 size-full origin-top-left select-none transition-transform duration-150 ease-out motion-reduce:transition-none"
            style={{ transform: transformOf(zoom) }}
          />
        ) : null}
      </div>
    </div>
  );
}

/** Zoom out · the level · zoom in · fit. Keys `-`, `=`, `0` do the same. */
export function ZoomControls({ zoom, onZoom }: { zoom: Zoom; onZoom: (next: Zoom) => void }) {
  return (
    <div role="group" aria-label="Zoom" className="flex items-center gap-1">
      <Button
        variant="ghost"
        size="icon"
        className="size-8"
        aria-label="Zoom out"
        aria-keyshortcuts="-"
        disabled={zoom.scale <= 1}
        onClick={() => onZoom(zoomOut(zoom))}
      >
        <Minus aria-hidden />
      </Button>
      <output aria-live="polite" className="w-12 text-center text-xs tabular-nums text-fg-muted">
        {percent(zoom)}
      </output>
      <Button
        variant="ghost"
        size="icon"
        className="size-8"
        aria-label="Zoom in"
        aria-keyshortcuts="="
        disabled={zoom.scale >= 6}
        onClick={() => onZoom(zoomIn(zoom))}
      >
        <Plus aria-hidden />
      </Button>
      <Button
        variant="ghost"
        size="icon"
        className="size-8"
        aria-label="Fit to frame"
        aria-keyshortcuts="0"
        disabled={zoom.scale === 1}
        onClick={() => onZoom(FIT)}
      >
        <Maximize aria-hidden />
      </Button>
    </div>
  );
}

export type ReferenceImage = { id: string; src: string; ratio: number; label: string };

/**
 * `ReferenceCompare` (Stage 04 PRD §15.4 H): the product reference beside the
 * generated asset, both aspect-true, at one synchronised zoom — the zoom the
 * centre stage also uses, so zooming anywhere zooms everywhere and the same
 * region of both files is on screen. `Product matches` is ticked against this.
 */
export function ReferenceCompare({
  references,
  asset,
  zoom,
  onZoom,
  emptyReason,
}: {
  references: ReferenceImage[];
  asset: { src: string; ratio: number; label: string } | null;
  zoom: Zoom;
  onZoom: (next: Zoom) => void;
  /** Said instead of a comparison when there is no reference to compare with. */
  emptyReason: string | null;
}) {
  const [picked, setPicked] = useState(0);
  const reference = references[Math.min(picked, references.length - 1)] ?? null;

  return (
    <section aria-labelledby="reference-compare-title" className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <h2 id="reference-compare-title" className="text-sm font-medium text-fg">
          Reference compare
        </h2>
        {reference && asset ? <ZoomControls zoom={zoom} onZoom={onZoom} /> : null}
      </div>

      {reference && asset ? (
        <>
          <div className="grid grid-cols-2 gap-2">
            <figure className="flex min-w-0 flex-col gap-1">
              <div className="aspect-square overflow-hidden rounded-token border">
                <ZoomedImage src={reference.src} ratio={reference.ratio} alt={reference.label} zoom={zoom} onZoom={onZoom} />
              </div>
              <figcaption className="truncate text-xs text-fg-muted">Reference · {reference.label}</figcaption>
            </figure>
            <figure className="flex min-w-0 flex-col gap-1">
              <div className="aspect-square overflow-hidden rounded-token border">
                <ZoomedImage src={asset.src} ratio={asset.ratio} alt={asset.label} zoom={zoom} onZoom={onZoom} />
              </div>
              <figcaption className="truncate text-xs text-fg-muted">Generated · {asset.label}</figcaption>
            </figure>
          </div>
          {references.length > 1 ? (
            <div role="group" aria-label="Reference" className="flex flex-wrap gap-1">
              {references.map((item, index) => (
                <Button
                  key={item.id}
                  size="sm"
                  variant={item.id === reference.id ? "secondary" : "ghost"}
                  aria-pressed={item.id === reference.id}
                  onClick={() => setPicked(index)}
                >
                  {item.label}
                </Button>
              ))}
            </div>
          ) : null}
        </>
      ) : (
        <p className="rounded-token border border-dashed px-3 py-2.5 text-sm text-fg-muted">{emptyReason}</p>
      )}
    </section>
  );
}
