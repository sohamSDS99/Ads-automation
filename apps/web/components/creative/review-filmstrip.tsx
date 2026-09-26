"use client";

import { Check, Circle, RotateCcw, X } from "lucide-react";
import { useEffect, useRef, type KeyboardEvent } from "react";

import { CHECKER_ID } from "@/components/creative/media-frame";
import { mediaContentUrl } from "@/lib/api/media-library";
import type { ReviewDecision, ReviewItem } from "@/lib/api/review";
import { DECISION_LABEL, type ReviewState } from "@/lib/creative/review";
import { cn } from "@/lib/utils";

const STATE_ICON: Record<ReviewDecision | "none", typeof Check> = {
  approve: Check,
  reject: X,
  regenerate: RotateCcw,
  none: Circle,
};

/** The icon's colour. The word beside it stays neutral: status colours are
 * UI colours, not text colours (success on white is 2.5:1), and the word —
 * not the hue — is what says the state (§15.2 rule 14). */
const STATE_TONE: Record<ReviewDecision | "none", string> = {
  approve: "text-status-success",
  reject: "text-status-failed-ink",
  regenerate: "text-status-gate-ink",
  none: "text-fg-subtle",
};

/** What a tile shows: an image's first rendition as its proxy, a video's poster. */
function thumbnail(item: ReviewItem): string | null {
  const first = item.renditions[0];
  if (!first) return null;
  return mediaContentUrl(first.media_id, item.kind === "video" ? "poster" : "preview");
}

/**
 * `ReviewFilmstrip` (Stage 04 PRD §15.4 H): the queue, one tile per asset
 * with its decision state in words and an icon — never colour alone (§15.2
 * rule 14). One tile is in the tab order at a time (roving tabindex): arrows
 * move within the strip, `J`/`K` move from anywhere.
 */
export function ReviewFilmstrip({
  items,
  state,
  cursor,
  onSelect,
  label,
}: {
  items: ReviewItem[];
  state: ReviewState;
  cursor: number;
  onSelect: (index: number) => void;
  label: (item: ReviewItem) => string;
}) {
  const list = useRef<HTMLOListElement>(null);

  // Keep the current tile on screen as J/K move it, without stealing focus.
  useEffect(() => {
    const tile = list.current?.querySelector<HTMLElement>(`[data-index="${cursor}"]`);
    tile?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [cursor]);

  const onKeyDown = (event: KeyboardEvent<HTMLOListElement>) => {
    const moves: Record<string, number> = {
      ArrowDown: 1,
      ArrowRight: 1,
      ArrowUp: -1,
      ArrowLeft: -1,
    };
    let next: number | null = null;
    if (event.key in moves) next = cursor + (moves[event.key] ?? 0);
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = items.length - 1;
    if (next === null) return;
    event.preventDefault();
    event.stopPropagation();
    const clamped = Math.max(0, Math.min(items.length - 1, next));
    onSelect(clamped);
    list.current?.querySelector<HTMLElement>(`[data-index="${clamped}"]`)?.focus();
  };

  return (
    <nav aria-label="Review queue" className="min-h-0 min-w-0 lg:flex-1">
      <ol
        ref={list}
        onKeyDown={onKeyDown}
        className="flex gap-1.5 overflow-x-auto p-0.5 pb-2 lg:h-full lg:flex-col lg:overflow-x-visible lg:overflow-y-auto lg:pb-0.5"
      >
        {items.map((item, index) => {
          const decision = state[item.asset_id]?.decision ?? null;
          const key = decision ?? "none";
          const Icon = STATE_ICON[key];
          const src = thumbnail(item);
          const current = index === cursor;
          return (
            <li key={item.asset_id} className="shrink-0 lg:shrink">
              <button
                type="button"
                data-index={index}
                tabIndex={current ? 0 : -1}
                aria-current={current ? "true" : undefined}
                onClick={() => onSelect(index)}
                className={cn(
                  "flex w-40 items-center gap-2.5 rounded-token border p-1.5 text-left transition-colors duration-150 lg:w-full",
                  current ? "border-accent bg-accent-soft" : "hover:bg-surface-hover",
                )}
              >
                <span className="relative size-12 shrink-0 overflow-hidden rounded-token border">
                  <svg aria-hidden className="absolute inset-0 size-full">
                    <rect width="100%" height="100%" fill={`url(#${CHECKER_ID})`} />
                  </svg>
                  {src ? (
                    // eslint-disable-next-line @next/next/no-img-element -- a signed 302 the optimiser cannot follow
                    <img
                      src={src}
                      alt=""
                      loading="lazy"
                      decoding="async"
                      className="absolute inset-0 size-full object-contain"
                    />
                  ) : null}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="flex items-baseline gap-1.5 text-xs tabular-nums text-fg-muted">
                    {String(index + 1).padStart(2, "0")}
                    <span className="truncate capitalize">{item.kind}</span>
                  </span>
                  <span className="block truncate text-xs text-fg">{label(item)}</span>
                  <span className={cn("mt-0.5 flex items-center gap-1 text-xs", current || decision ? "text-fg" : "text-fg-muted")}>
                    <Icon aria-hidden className={cn("size-3.5 shrink-0", STATE_TONE[key])} />
                    {decision ? DECISION_LABEL[decision] : "Undecided"}
                  </span>
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
