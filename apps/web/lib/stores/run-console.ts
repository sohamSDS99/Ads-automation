/**
 * The two pieces of console state that do not belong in the query cache.
 *
 * The log is written far faster than anything else on the screen — a node can
 * emit progress in a loop — so it lives here, where only the drawer subscribes.
 * Putting it in React state would re-render the DAG canvas on every line, which
 * is exactly the 16ms budget PRD §13.5 #1 sets.
 */
"use client";

import { create } from "zustand";

import type { RunEvent } from "@/lib/run-events";

/** Kept in memory, so it is bounded. The durable record is the API. */
export const MAX_LOG_LINES = 1000;

export type LogLine = {
  id: string;
  at: number;
  type: RunEvent["type"];
  nodeId: string | null;
  text: string;
};

type ConsoleState = {
  runId: string | null;
  lines: LogLine[];
  /** Scoped to one run: opening another console starts an empty log. */
  attach: (runId: string) => void;
  append: (line: LogLine) => void;
  clear: () => void;
};

export const useRunConsole = create<ConsoleState>((set, get) => ({
  runId: null,
  lines: [],
  attach: (runId) => {
    if (get().runId === runId) return;
    set({ runId, lines: [] });
  },
  append: (line) =>
    set((state) => {
      const lines = [...state.lines, line];
      return { lines: lines.length > MAX_LOG_LINES ? lines.slice(-MAX_LOG_LINES) : lines };
    }),
  clear: () => set({ lines: [] }),
}));
