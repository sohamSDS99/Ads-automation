"use client";

import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";

import { FrameCheckStrip } from "@/components/creative/frame-check-strip";
import { LetterboxFrame } from "@/components/creative/media-frame";
import { Badge } from "@/components/ui/badge";
import { mediaContentUrl, type CampaignVideo, type VideoRendition } from "@/lib/api/media-library";
import {
  atPercent,
  captionMarkers,
  endCardRegion,
  pxLabel,
  ratioValue,
  secondsLabel,
} from "@/lib/creative/media-library";
import { cn } from "@/lib/utils";

/** A `<video>`'s signed URL lives 300 s; a seek after that is re-signed, twice at most. */
const MAX_RESIGN = 2;

/**
 * `VideoPlayer` (Stage 04 PRD §15.4 G, §15.5 item 3): the 480p proxy — never
 * the master, which loads only in the detail drawer — muted by default, with
 * `preload="metadata"`, streamed from the file server and seeked with `Range`.
 *
 * Under it, the timeline this file was verified against: the 0–5 s brand
 * window as a band with the moment the brand first shows, the logo-detection
 * samples as ticks (solid: found; hollow: not found), each caption's interval
 * with the OCR score verify.py measured for it, and the end card. Every mark
 * is the server's number; the list beneath says each in words.
 */
export function VideoPlayer({ video, rendition }: { video: CampaignVideo; rendition: VideoRendition }) {
  const element = useRef<HTMLVideoElement>(null);
  const [nowMs, setNowMs] = useState(0);
  const [resigned, setResigned] = useState(0);
  const [failed, setFailed] = useState<string | null>(null);
  const resumeAt = useRef<number | null>(null);
  const duration = rendition.duration_ms;
  const ratio = ratioValue(rendition.ratio) ?? 16 / 9;

  const src = `${mediaContentUrl(rendition.media_id, "preview")}${resigned ? `&signed=${resigned}` : ""}`;

  // Muted as a property and as the default, whatever React does with the
  // attribute: a Media Library that starts speaking is not a quiet tool.
  useEffect(() => {
    const node = element.current;
    if (!node) return;
    node.muted = true;
    node.defaultMuted = true;
  }, []);

  useEffect(() => {
    const node = element.current;
    if (!node) return;
    let frame = 0;
    const tick = () => {
      setNowMs(node.currentTime * 1000);
      if (!node.paused) frame = requestAnimationFrame(tick);
    };
    const onPlay = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(tick);
    };
    const onTime = () => setNowMs(node.currentTime * 1000);
    node.addEventListener("play", onPlay);
    node.addEventListener("seeked", onTime);
    node.addEventListener("timeupdate", onTime);
    return () => {
      cancelAnimationFrame(frame);
      node.removeEventListener("play", onPlay);
      node.removeEventListener("seeked", onTime);
      node.removeEventListener("timeupdate", onTime);
    };
  }, []);

  const seek = useCallback(
    (ms: number) => {
      const node = element.current;
      if (!node) return;
      const clamped = Math.min(Math.max(0, ms), duration);
      node.currentTime = clamped / 1000;
      setNowMs(clamped);
    },
    [duration],
  );

  const onError = () => {
    const node = element.current;
    if (resigned < MAX_RESIGN) {
      resumeAt.current = node ? node.currentTime : null;
      setResigned((count) => count + 1);
      return;
    }
    setFailed(
      node?.error?.message ||
        "The proxy could not be read from the file server. Reload the page to sign it again.",
    );
  };

  const onLoadedMetadata = () => {
    const node = element.current;
    if (node && resumeAt.current !== null) {
      node.currentTime = resumeAt.current;
      resumeAt.current = null;
    }
  };

  const captions = captionMarkers(video.script.captions, rendition.verification.caption_frames);
  const endCard = endCardRegion(duration, rendition.end_card_ms);
  const brandWindow = rendition.brand_window_ms ?? null;
  const logoFrames = rendition.verification.logo_frames;
  const found = logoFrames.filter((frame) => frame.detected).length;

  return (
    <section className="flex min-w-0 flex-col gap-3" data-testid="video-player" data-ratio={rendition.ratio}>
      <div className="mx-auto w-full max-w-3xl">
        <LetterboxFrame ratio={ratio} frameRatio={16 / 9}>
          <video
            ref={element}
            src={src}
            poster={mediaContentUrl(rendition.media_id, "poster")}
            muted
            playsInline
            controls
            preload="metadata"
            onError={onError}
            onLoadedMetadata={onLoadedMetadata}
            aria-label={`${video.campaign_ref} video, ${rendition.ratio}, 480p proxy`}
            className="block size-full bg-bg"
          />
        </LetterboxFrame>
      </div>
      {failed ? <p className="text-sm text-status-failed-ink">{failed}</p> : null}

      <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs tabular-nums text-fg-muted">
        <span className="font-medium text-fg">{rendition.ratio}</span>
        <span>{pxLabel(rendition.px)} master</span>
        <span>480p proxy</span>
        <span>{secondsLabel(duration)}</span>
        <Badge>{rendition.derivation === "native" ? "Native" : `Cropped from ${rendition.source_ratio}`}</Badge>
        <Badge tone={rendition.verification.passed ? "neutral" : "danger"}>
          {rendition.verification.passed ? "Verified" : "Verification failed"}
        </Badge>
        <span>{rendition.has_audio ? "Sound, loudness-normalised" : "Silent track"}</span>
      </p>

      <Timeline
        durationMs={duration}
        nowMs={nowMs}
        onSeek={seek}
        brandWindowMs={brandWindow}
        brandFirstMs={rendition.brand_first_at_ms}
        logoFrames={logoFrames}
        captions={captions}
        endCard={endCard}
      />

      <ul className="grid gap-x-6 gap-y-1 text-xs text-fg-muted sm:grid-cols-2" aria-label="Timeline, in words">
        <li>
          <span className="font-medium text-fg">Brand window</span>{" "}
          {brandWindow === null
            ? "not recorded for this run"
            : `0–${secondsLabel(brandWindow)}; the brand first shows at ${secondsLabel(rendition.brand_first_at_ms)}`}
        </li>
        <li>
          <span className="font-medium text-fg">Logo samples</span> {found} of {logoFrames.length} found (1 fps,
          first five seconds)
        </li>
        <li>
          <span className="font-medium text-fg">Captions</span>{" "}
          {captions.length === 0
            ? "none in the script"
            : `${captions.length}, burned in${
                rendition.caption_ocr_min_similarity === null
                  ? ""
                  : `; lowest OCR ${rendition.caption_ocr_min_similarity.toFixed(2)}`
              }`}
        </li>
        <li>
          <span className="font-medium text-fg">End card</span>{" "}
          {endCard
            ? `${secondsLabel(endCard.startMs)}–${secondsLabel(endCard.endMs)}`
            : "length not recorded for this run"}
        </li>
      </ul>
      {rendition.verification.failures.length > 0 ? (
        <ul className="list-disc pl-5 text-xs text-status-failed-ink">
          {rendition.verification.failures.map((failure) => (
            <li key={failure}>{failure}</li>
          ))}
        </ul>
      ) : null}

      <FrameCheckStrip
        src={mediaContentUrl(rendition.media_id, "preview")}
        ratio={ratio}
        frames={logoFrames}
        onSeek={seek}
      />
    </section>
  );
}

type TimelineProps = {
  durationMs: number;
  nowMs: number;
  onSeek: (ms: number) => void;
  brandWindowMs: number | null;
  brandFirstMs: number;
  logoFrames: VideoRendition["verification"]["logo_frames"];
  captions: ReturnType<typeof captionMarkers>;
  endCard: { startMs: number; endMs: number } | null;
};

const STEP_MS = 1000;
const PAGE_MS = 5000;

function Timeline({
  durationMs,
  nowMs,
  onSeek,
  brandWindowMs,
  brandFirstMs,
  logoFrames,
  captions,
  endCard,
}: TimelineProps) {
  const track = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);

  const fromPointer = (event: PointerEvent<HTMLDivElement>) => {
    const box = track.current?.getBoundingClientRect();
    if (!box || box.width === 0) return;
    onSeek(((event.clientX - box.left) / box.width) * durationMs);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const moves: Record<string, number> = {
      ArrowRight: STEP_MS,
      ArrowUp: STEP_MS,
      ArrowLeft: -STEP_MS,
      ArrowDown: -STEP_MS,
      PageUp: PAGE_MS,
      PageDown: -PAGE_MS,
    };
    if (event.key === "Home") onSeek(0);
    else if (event.key === "End") onSeek(durationMs);
    else if (event.key in moves) onSeek(nowMs + (moves[event.key] ?? 0));
    else return;
    event.preventDefault();
  };

  const seconds = Math.floor(durationMs / 1000);
  const every = seconds > 20 ? 5 : seconds > 10 ? 2 : 1;

  return (
    <div className="flex min-w-0 flex-col gap-1.5" data-testid="video-timeline">
      <div className="flex min-w-0 gap-3">
        <div aria-hidden className="flex w-16 shrink-0 flex-col justify-around text-xs text-fg-muted">
          <span className="h-5 leading-5">Brand</span>
          <span className="h-5 leading-5">Captions</span>
          <span className="h-5 leading-5">End card</span>
        </div>
        <div
          ref={track}
          role="slider"
          tabIndex={0}
          aria-label="Seek the video"
          aria-valuemin={0}
          aria-valuemax={Math.round(durationMs / 100) / 10}
          aria-valuenow={Math.round(nowMs / 100) / 10}
          aria-valuetext={`${secondsLabel(Math.round(nowMs / 100) * 100)} of ${secondsLabel(durationMs)}`}
          onKeyDown={onKeyDown}
          onPointerDown={(event) => {
            dragging.current = true;
            event.currentTarget.setPointerCapture(event.pointerId);
            fromPointer(event);
          }}
          onPointerMove={(event) => {
            if (dragging.current) fromPointer(event);
          }}
          onPointerUp={() => {
            dragging.current = false;
          }}
          className={cn(
            "relative flex min-w-0 flex-1 cursor-pointer touch-none select-none flex-col justify-around gap-1",
            "rounded-token border bg-surface px-0 py-1",
            "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
          )}
        >
          {/* Brand lane: the window, the first sighting, the 1 fps samples. */}
          <div className="relative h-5">
            {brandWindowMs !== null ? (
              <div
                data-mark="brand-window"
                data-start-ms={0}
                data-end-ms={brandWindowMs}
                className="absolute inset-y-0 left-0 rounded-sm bg-accent-soft"
                style={{ width: `${atPercent(brandWindowMs, durationMs)}%` }}
              />
            ) : null}
            <div
              data-mark="brand-first"
              data-ms={brandFirstMs}
              className="absolute inset-y-0 w-0.5 bg-accent"
              style={{ left: `${atPercent(brandFirstMs, durationMs)}%` }}
            />
            {logoFrames.map((frame) => (
              <span
                key={frame.t_ms}
                data-mark="logo-frame"
                data-ms={frame.t_ms}
                data-detected={frame.detected ? "true" : "false"}
                title={`${secondsLabel(frame.t_ms)}: logo ${frame.detected ? "found" : "not found"}, score ${frame.score.toFixed(2)}`}
                className={cn(
                  "absolute bottom-0 w-1 -translate-x-1/2 rounded-sm",
                  frame.detected ? "h-4 bg-fg" : "h-2 border border-fg-muted bg-surface",
                )}
                style={{ left: `${atPercent(frame.t_ms, durationMs)}%` }}
              />
            ))}
          </div>
          {/* Caption lane: each interval, with what OCR read of it. */}
          <div className="relative h-5">
            {captions.map((caption) => (
              <span
                key={caption.index}
                data-mark="caption"
                data-start-ms={caption.startMs}
                data-end-ms={caption.endMs}
                data-ocr={caption.ocr ? caption.ocr.similarity.toFixed(2) : "none"}
                title={`“${caption.text}” ${secondsLabel(caption.startMs)}–${secondsLabel(caption.endMs)}${
                  caption.ocr
                    ? ` · OCR ${caption.ocr.similarity.toFixed(2)} ${caption.ocr.passed ? "passes" : "fails"}: read “${caption.ocr.read}”`
                    : " · not sampled"
                }`}
                className={cn(
                  "absolute inset-y-0 overflow-hidden rounded-sm px-1 text-xs leading-5 tabular-nums",
                  caption.ocr && !caption.ocr.passed
                    ? "border border-dashed border-status-failed text-status-failed-ink"
                    : "bg-series-1 text-accent-fg",
                )}
                style={{
                  left: `${atPercent(caption.startMs, durationMs)}%`,
                  width: `${atPercent(caption.endMs, durationMs) - atPercent(caption.startMs, durationMs)}%`,
                }}
              >
                {caption.ocr ? caption.ocr.similarity.toFixed(2) : "—"}
              </span>
            ))}
          </div>
          {/* End-card lane. */}
          <div className="relative h-5">
            {endCard ? (
              <span
                data-mark="end-card"
                data-start-ms={endCard.startMs}
                data-end-ms={endCard.endMs}
                className="absolute inset-y-0 overflow-hidden rounded-sm border border-border-strong bg-surface-hover px-1 text-xs leading-5 text-fg-muted"
                style={{
                  left: `${atPercent(endCard.startMs, durationMs)}%`,
                  width: `${atPercent(endCard.endMs, durationMs) - atPercent(endCard.startMs, durationMs)}%`,
                }}
              >
                End card
              </span>
            ) : null}
          </div>
          <span
            aria-hidden
            data-mark="playhead"
            className="pointer-events-none absolute inset-y-0 w-0.5 bg-fg"
            style={{ left: `${atPercent(nowMs, durationMs)}%` }}
          />
        </div>
      </div>
      <div aria-hidden className="flex gap-3">
        <span className="w-16 shrink-0" />
        <div className="relative h-4 flex-1 text-xs tabular-nums text-fg-subtle">
          {Array.from({ length: Math.floor(seconds / every) + 1 }, (_, i) => i * every * 1000).map((ms) => (
            <span
              key={ms}
              className="absolute -translate-x-1/2"
              style={{ left: `${atPercent(ms, durationMs)}%` }}
            >
              {ms / 1000}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}
