"use client";

import { useEffect, useRef, useState } from "react";

import type { LogoFrame } from "@/lib/api/media-library";
import { secondsLabel } from "@/lib/creative/media-library";
import { cn } from "@/lib/utils";

const THUMB_WIDTH = 112;

/**
 * `FrameCheckStrip` (Stage 04 PRD §15.4 G): the 1 fps samples verify.py took
 * from the first five seconds, each with its logo-match score — the evidence
 * behind the brand band. The frames themselves are drawn from the same 480p
 * proxy the player streams (seeked with `Range`), at exactly the sampled
 * times; the scores are the server's. A frame opens the player at its time.
 */
export function FrameCheckStrip({
  src,
  ratio,
  frames,
  onSeek,
}: {
  src: string;
  ratio: number;
  frames: LogoFrame[];
  onSeek: (ms: number) => void;
}) {
  const canvases = useRef<(HTMLCanvasElement | null)[]>([]);
  const [drawn, setDrawn] = useState(0);
  const [failure, setFailure] = useState<string | null>(null);
  const height = Math.round(THUMB_WIDTH / ratio);

  useEffect(() => {
    if (frames.length === 0) return;
    const reader = document.createElement("video");
    reader.muted = true;
    reader.preload = "metadata";
    reader.playsInline = true;
    let index = 0;
    let cancelled = false;
    const next = () => {
      if (cancelled || index >= frames.length) return;
      reader.currentTime = (frames[index]?.t_ms ?? 0) / 1000;
    };
    const onSeeked = () => {
      if (cancelled) return;
      const canvas = canvases.current[index];
      const context = canvas?.getContext("2d");
      if (canvas && context) context.drawImage(reader, 0, 0, canvas.width, canvas.height);
      index += 1;
      setDrawn(index);
      next();
    };
    const onError = () => {
      if (!cancelled) setFailure("The sampled frames could not be read from the proxy; the scores stand.");
    };
    reader.addEventListener("loadedmetadata", next);
    reader.addEventListener("seeked", onSeeked);
    reader.addEventListener("error", onError);
    reader.src = src;
    return () => {
      cancelled = true;
      reader.removeEventListener("loadedmetadata", next);
      reader.removeEventListener("seeked", onSeeked);
      reader.removeEventListener("error", onError);
      reader.removeAttribute("src");
      reader.load();
    };
  }, [src, frames]);

  if (frames.length === 0) {
    return <p className="text-xs text-fg-muted">verify.py recorded no frame samples for this file.</p>;
  }

  return (
    <section className="flex min-w-0 flex-col gap-2" aria-label="Frame check, first five seconds" data-testid="frame-check-strip">
      <p className="text-xs text-fg-muted">
        <span className="font-medium text-fg">Frame check</span> · 1 fps, first five seconds
        {drawn < frames.length && !failure ? ` · reading frame ${drawn + 1} of ${frames.length}` : ""}
      </p>
      <ol className="flex gap-2 overflow-x-auto pb-1">
        {frames.map((frame, i) => (
          <li key={frame.t_ms} className="shrink-0">
            <button
              type="button"
              onClick={() => onSeek(frame.t_ms)}
              data-frame-ms={frame.t_ms}
              data-detected={frame.detected ? "true" : "false"}
              aria-label={`${secondsLabel(frame.t_ms)}: logo ${frame.detected ? "found" : "not found"}, match ${frame.score.toFixed(2)}. Play from here`}
              className={cn(
                "flex flex-col gap-1 rounded-token p-1 text-left text-xs",
                "transition-colors duration-150 ease-out hover:bg-surface-hover motion-reduce:transition-none",
                "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
              )}
            >
              <canvas
                ref={(node) => {
                  canvases.current[i] = node;
                }}
                width={THUMB_WIDTH}
                height={height}
                className={cn("block rounded-sm border bg-surface", frame.detected ? "border-fg" : "border-dashed")}
                style={{ width: THUMB_WIDTH, height }}
              />
              <span className="flex items-baseline justify-between gap-2 tabular-nums">
                <span className="font-medium text-fg">{secondsLabel(frame.t_ms)}</span>
                <span className="text-fg-muted">{frame.score.toFixed(2)}</span>
              </span>
              <span className={frame.detected ? "text-fg" : "text-fg-muted"}>
                {frame.detected ? "Logo found" : "Not found"}
              </span>
            </button>
          </li>
        ))}
      </ol>
      {failure ? <p className="text-xs text-fg-muted">{failure}</p> : null}
    </section>
  );
}
