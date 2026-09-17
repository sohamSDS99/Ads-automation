"use client";

import { useVirtualizer } from "@tanstack/react-virtual";
import { ChevronDown, ChevronUp } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { MAX_LOG_LINES, useRunConsole } from "@/lib/stores/run-console";
import { cn } from "@/lib/utils";

/**
 * The run's own narration (PRD §13.4 B, log drawer).
 *
 * Virtualized because a long run emits hundreds of lines and the drawer is a
 * background surface — it must not cost frames while someone is reading the
 * panel next to it (PRD §13.5 #1). Auto-follows the tail until the reader
 * scrolls up, then stays where they left it: a log that yanks itself back to
 * the bottom while you are reading is worse than no log.
 */
const ROW_HEIGHT = 22;

export function LogDrawer({ className }: { className?: string }) {
  const lines = useRunConsole((state) => state.lines);
  const [open, setOpen] = useState(false);
  const [following, setFollowing] = useState(true);
  const viewport = useRef<HTMLDivElement>(null);

  const rows = useVirtualizer({
    count: lines.length,
    getScrollElement: () => viewport.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
  });

  useEffect(() => {
    if (open && following && lines.length > 0) {
      rows.scrollToIndex(lines.length - 1, { align: "end" });
    }
  }, [open, following, lines.length, rows]);

  return (
    <section className={cn("border-t bg-surface", className)} aria-label="Run log">
      <h2>
        <button
          type="button"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          className="flex w-full items-center gap-2 px-4 py-2 text-left text-sm text-fg-muted transition-colors hover:text-fg"
        >
          {open ? (
            <ChevronDown className="size-4" aria-hidden />
          ) : (
            <ChevronUp className="size-4" aria-hidden />
          )}
          <span className="font-medium">Log</span>
          <span data-numeric className="text-xs text-fg-subtle">
            {lines.length === MAX_LOG_LINES ? `last ${MAX_LOG_LINES}` : lines.length} lines
          </span>
        </button>
      </h2>

      {open ? (
        <div
          ref={viewport}
          onScroll={(event) => {
            const element = event.currentTarget;
            const atBottom =
              element.scrollHeight - element.scrollTop - element.clientHeight < ROW_HEIGHT;
            setFollowing(atBottom);
          }}
          className="h-48 overflow-y-auto border-t px-4 py-2 font-mono text-xs"
        >
          {lines.length === 0 ? (
            <p className="text-fg-subtle">Nothing yet. Lines arrive as the run works.</p>
          ) : (
            <div style={{ height: rows.getTotalSize(), position: "relative" }}>
              {rows.getVirtualItems().map((row) => {
                const line = lines[row.index]!;
                return (
                  <div
                    key={line.id}
                    className="absolute inset-x-0 flex gap-2 leading-[22px] whitespace-nowrap"
                    style={{ transform: `translateY(${row.start}px)` }}
                  >
                    <time
                      className="shrink-0 text-fg-subtle"
                      dateTime={new Date(line.at).toISOString()}
                    >
                      {clock(line.at)}
                    </time>
                    <span
                      className={cn(
                        "truncate",
                        line.type === "node.failed" ? "text-status-failed" : "text-fg-muted",
                      )}
                    >
                      {line.text}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      ) : null}
    </section>
  );
}

function clock(at: number): string {
  return new Date(at).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}
