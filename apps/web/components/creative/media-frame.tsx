import type { ReactNode } from "react";

import { letterbox } from "@/lib/creative/media-library";
import { cn } from "@/lib/utils";

/** The one checkerboard every frame in the Media Library references. */
export const CHECKER_ID = "media-checker";

/**
 * The neutral checkerboard behind every letterboxed file (§15.4 G): two flat
 * token surfaces in an 8 px check, defined once and referenced by each frame,
 * so a transparent logo reads as transparent and the bars around a wide image
 * read as "not part of the file". Rendered once by the Media Library.
 */
export function CheckerDefs() {
  return (
    <svg aria-hidden width="0" height="0" className="absolute">
      <defs>
        <pattern id={CHECKER_ID} width="16" height="16" patternUnits="userSpaceOnUse">
          <rect width="16" height="16" className="fill-surface" />
          <rect width="8" height="8" className="fill-surface-hover" />
          <rect x="8" y="8" width="8" height="8" className="fill-surface-hover" />
        </pattern>
      </defs>
    </svg>
  );
}

/**
 * A frame of `frameRatio` on the checkerboard, holding a box of the file's
 * TRUE ratio, as large as fits — letterboxed, never cropped: nothing in the
 * Media Library uses `object-cover` (§15.4 G). `children` fill the box.
 */
export function LetterboxFrame({
  ratio,
  frameRatio = 1,
  children,
  className,
  boxClassName,
  dashed = false,
}: {
  /** width / height of the file (or, for a gap, of the ratio it lacks). */
  ratio: number;
  frameRatio?: number;
  children?: ReactNode;
  className?: string;
  boxClassName?: string;
  /** A gap: the box is a dashed outline of the missing shape. */
  dashed?: boolean;
}) {
  const box = letterbox(ratio, frameRatio);
  return (
    <div
      className={cn("relative grid w-full place-items-center overflow-hidden rounded-token border", className)}
      style={{ aspectRatio: String(frameRatio) }}
    >
      {dashed ? null : (
        <svg aria-hidden className="absolute inset-0 size-full">
          <rect width="100%" height="100%" fill={`url(#${CHECKER_ID})`} />
        </svg>
      )}
      <div
        data-letterbox=""
        className={cn(
          "relative",
          dashed ? "rounded-token border-2 border-dashed border-border-strong" : "",
          boxClassName,
        )}
        style={{ width: `${box.width}%`, height: `${box.height}%` }}
      >
        {children}
      </div>
    </div>
  );
}
