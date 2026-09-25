"use client";

import { ImageOff } from "lucide-react";
import { useState } from "react";

import type { LandingDevice, OfferAboveFold } from "@/lib/api/creative-runs";
import { cn } from "@/lib/utils";

const DEVICE: Record<LandingDevice, string> = { mobile: "Mobile", desktop: "Desktop" };

/** How much page to show under the first screen before the frame scrolls. */
const BELOW_FOLD = 0.35;

/**
 * `FoldOverlay` — one device's full-page capture with what 4.5.2 measured
 * drawn on it (Stage 04 PRD §15.4 J): the fold line, the offer's bounding box,
 * and the first screen marked when a fixed overlay covers more than 30% of it.
 *
 * **Measured, never re-derived.** Every coordinate is the renderer's: `fold`
 * is its `window.innerHeight`, `offer.bbox` the offer text node's box in page
 * CSS px at scroll 0. The renderer sets no device scale factor, so one capture
 * pixel is one CSS px, and the overlay is an SVG whose viewBox is the capture's
 * own pixel size laid exactly over the image — it scales with the image and
 * cannot drift from it. The capture keeps its true aspect ratio (rule 1); the
 * frame opens on the first screen and the offer, and scrolls for the rest.
 */
export function FoldOverlay({
  device,
  src,
  fold,
  offer,
  obscured,
  className,
}: {
  device: LandingDevice;
  /** Null when the page never rendered on this device. */
  src: string | null;
  fold: number | null;
  /** This device's offer check; null when the brief carries no offer. */
  offer: OfferAboveFold | null;
  obscured: boolean;
  className?: string;
}) {
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  const [broken, setBroken] = useState(false);
  const box = offer?.bbox ?? null;
  const summary = [
    fold !== null ? `fold at ${fold} px` : "no fold measured",
    offer
      ? offer.found
        ? "offer above the fold"
        : box
          ? `offer below the fold, ${Math.round(box.y)} px down`
          : "offer not on the page"
      : null,
    obscured ? "an overlay covers over 30% of the first screen" : null,
  ].filter(Boolean);

  if (!src || broken) {
    return (
      <figure className={cn("flex min-w-0 flex-col gap-2", className)} data-testid={`fold-${device}`}>
        <figcaption className="text-xs text-fg-muted">
          <span className="font-medium text-fg">{DEVICE[device]}</span>
        </figcaption>
        <div className="flex min-h-40 flex-col items-center justify-center gap-2 rounded-token border border-dashed px-4 py-6 text-center text-sm text-fg-muted">
          <ImageOff className="size-5" aria-hidden />
          {broken
            ? `The ${device} capture could not be loaded. Refresh to try again.`
            : `The page did not render on ${device}, so there is no capture.`}
        </div>
      </figure>
    );
  }

  // The frame opens on the first screen and the offer, whichever reaches lower.
  const reach = Math.max(fold ?? 0, box ? box.y + box.height : 0);
  const shown = size ? Math.min(size.height, reach > 0 ? reach * (1 + BELOW_FOLD) : size.height) : null;
  const pct = (value: number, of: number) => `${(value / of) * 100}%`;

  return (
    <figure className={cn("flex min-w-0 flex-col gap-2", className)} data-testid={`fold-${device}`} data-device={device}>
      <figcaption className="text-xs text-fg-muted">
        <span className="font-medium text-fg">{DEVICE[device]}</span>
        {size ? <span className="tabular-nums"> · {size.width} px wide</span> : null} · {summary.join(" · ")}
      </figcaption>
      <div
        tabIndex={0}
        role="region"
        aria-label={`${DEVICE[device]} capture: ${summary.join(", ")}`}
        className="overflow-y-auto rounded-token border bg-surface focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        style={size && shown ? { aspectRatio: `${size.width} / ${shown}` } : undefined}
      >
        <div className="relative">
          {/* eslint-disable-next-line @next/next/no-img-element -- a capture served by the api, not a static asset */}
          <img
            src={src}
            alt={`The landing page as rendered on ${device}`}
            data-testid={`capture-${device}`}
            className="block h-auto w-full"
            onLoad={(event) =>
              setSize({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })
            }
            onError={() => setBroken(true)}
          />
          {size ? (
            <>
              <svg
                aria-hidden
                data-testid={`overlay-${device}`}
                className="pointer-events-none absolute inset-0 size-full"
                viewBox={`0 0 ${size.width} ${size.height}`}
                preserveAspectRatio="none"
              >
                <defs>
                  <pattern id={`obscured-${device}`} width="12" height="12" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
                    <line x1="0" y1="0" x2="0" y2="12" stroke="var(--status-gate)" strokeWidth="4" strokeOpacity="0.45" />
                  </pattern>
                </defs>
                {obscured && fold !== null ? (
                  <rect data-testid={`obscured-${device}`} x="0" y="0" width={size.width} height={fold} fill={`url(#obscured-${device})`} />
                ) : null}
                {fold !== null ? (
                  <g data-testid={`fold-line-${device}`} data-y={fold}>
                    <line x1="0" x2={size.width} y1={fold} y2={fold} stroke="var(--surface-raised)" strokeWidth="5" vectorEffect="non-scaling-stroke" />
                    <line x1="0" x2={size.width} y1={fold} y2={fold} stroke="var(--fg)" strokeWidth="2" strokeDasharray="6 4" vectorEffect="non-scaling-stroke" />
                  </g>
                ) : null}
                {box ? (
                  <g>
                    <rect x={box.x} y={box.y} width={box.width} height={box.height} fill="none" stroke="var(--surface-raised)" strokeWidth="5" vectorEffect="non-scaling-stroke" />
                    <rect
                      data-testid={`offer-box-${device}`}
                      data-found={offer?.found ? "true" : "false"}
                      x={box.x}
                      y={box.y}
                      width={box.width}
                      height={box.height}
                      fill="none"
                      stroke={offer?.found ? "var(--status-success)" : "var(--status-failed)"}
                      strokeWidth="2"
                      vectorEffect="non-scaling-stroke"
                    />
                  </g>
                ) : null}
              </svg>
              {fold !== null ? (
                <span
                  className="pointer-events-none absolute left-1 -translate-y-full rounded-sm bg-surface-raised px-1 text-xs font-medium text-fg tabular-nums"
                  style={{ top: pct(fold, size.height) }}
                >
                  Fold · {fold} px
                </span>
              ) : null}
              {box ? (
                <span
                  className="pointer-events-none absolute inline-flex items-center gap-1 rounded-sm bg-surface-raised px-1 text-xs font-medium text-fg"
                  style={{ top: pct(box.y + box.height, size.height), left: pct(box.x, size.width) }}
                >
                  <span aria-hidden className={cn("size-2 rounded-full", offer?.found ? "bg-status-success" : "bg-status-failed")} />
                  Offer · {offer?.found ? "above the fold" : "below the fold"}
                </span>
              ) : null}
              {obscured && fold !== null ? (
                <span
                  className="pointer-events-none absolute right-1 -translate-y-full rounded-sm bg-surface-raised px-1 text-xs font-medium text-fg"
                  style={{ top: pct(fold, size.height) }}
                >
                  Overlay covers over 30%
                </span>
              ) : null}
            </>
          ) : null}
        </div>
      </div>
    </figure>
  );
}
