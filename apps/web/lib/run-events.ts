/**
 * The run console's feed (PRD §7.3, §13.5 #2).
 *
 * `EventSource` rather than a hand-rolled fetch stream: it reconnects on its
 * own and replays `Last-Event-ID` for us, which is the whole contract the API's
 * SSE endpoint was built around. It cannot set headers — hence the
 * `last_event_id` query parameter for the *first* connection, which is the one
 * case the browser has no header for.
 *
 * Two rules keep this from lying to the person watching:
 *
 * 1. **Every reconnect reconciles.** A dropped connection may have dropped
 *    events, and no event announces its own absence, so the console refetches
 *    the whole run and trusts that over anything it accumulated.
 * 2. **The terminal event closes the stream deliberately.** The server ends the
 *    response after `run.completed`; left alone, `EventSource` reads that as a
 *    failure and reconnects forever.
 */
"use client";

import { useEffect, useRef, useState } from "react";

import { API_BASE } from "@/lib/api";

export type RunEventType =
  | "run.status"
  | "node.started"
  | "node.progress"
  | "node.tokens"
  | "node.completed"
  | "node.failed"
  | "approval.required"
  | "run.completed"
  | "export.ready";

export const RUN_EVENT_TYPES: RunEventType[] = [
  "run.status",
  "node.started",
  "node.progress",
  "node.tokens",
  "node.completed",
  "node.failed",
  "approval.required",
  "run.completed",
  "export.ready",
];

export type RunEvent = {
  id: string;
  type: RunEventType;
  data: Record<string, unknown>;
};

export type StreamState = "connecting" | "live" | "reconnecting" | "closed";

type Options = {
  runId: string;
  /** Off for a run that finished long ago and has nothing left to say. */
  enabled: boolean;
  onEvent: (event: RunEvent) => void;
  /** Called on every connection after the first — the reconciliation point. */
  onReconnect: () => void;
};

export function useRunStream({ runId, enabled, onEvent, onReconnect }: Options): StreamState {
  const [state, setState] = useState<StreamState>(enabled ? "connecting" : "closed");
  // Held in refs so a changing callback identity cannot tear down a live
  // stream: re-subscribing would replay the run from the last cursor and
  // double every log line.
  const handleEvent = useRef(onEvent);
  const handleReconnect = useRef(onReconnect);
  handleEvent.current = onEvent;
  handleReconnect.current = onReconnect;

  useEffect(() => {
    if (!enabled) {
      setState("closed");
      return;
    }

    let cursor: string | null = null;
    let opened = 0;
    let closed = false;
    let source: EventSource | null = null;

    const open = () => {
      const query = cursor ? `?last_event_id=${encodeURIComponent(cursor)}` : "";
      source = new EventSource(`${API_BASE}/runs/${runId}/events${query}`);

      source.onopen = () => {
        opened += 1;
        setState("live");
        if (opened > 1) handleReconnect.current();
      };

      source.onerror = () => {
        // `EventSource` retries by itself; this only reports the gap. A server
        // that closed the stream cleanly lands here too, which is why the
        // terminal event closes it explicitly below.
        if (!closed) setState("reconnecting");
      };

      for (const type of RUN_EVENT_TYPES) {
        source.addEventListener(type, (message) => {
          const event = message as MessageEvent<string>;
          cursor = event.lastEventId || cursor;
          let data: Record<string, unknown> = {};
          try {
            data = JSON.parse(event.data) as Record<string, unknown>;
          } catch {
            // A frame we cannot read is worth a log line, never a crash.
            data = { raw: event.data };
          }
          handleEvent.current({ id: event.lastEventId, type, data });
          if (type === "run.completed") {
            closed = true;
            source?.close();
            setState("closed");
          }
        });
      }
    };

    open();
    return () => {
      closed = true;
      source?.close();
    };
  }, [runId, enabled]);

  return state;
}

/** The sentence a feed event becomes in the log drawer. */
export function describe(event: RunEvent): string {
  const data = event.data;
  const node = typeof data.node_id === "string" ? data.node_id : "";
  switch (event.type) {
    case "run.status":
      return `Run ${String(data.status ?? "")}`;
    case "node.started":
      return `${node} started${data.attempt && Number(data.attempt) > 1 ? ` (attempt ${String(data.attempt)})` : ""}`;
    case "node.progress":
      return `${node} ${String(data.message ?? "")}`;
    case "node.tokens":
      return `${node} used ${String(data.token_in ?? 0)} in / ${String(data.token_out ?? 0)} out`;
    case "node.completed":
      return `${node} succeeded${data.cached ? " (cached)" : ""}`;
    case "node.failed":
      return `${node} failed${data.will_retry ? ", retrying" : ""}${errorSuffix(data.error)}`;
    case "approval.required":
      return `${node} is waiting on an approval`;
    case "run.completed":
      return `Run ${String(data.status ?? "finished")}`;
    case "export.ready":
      return `Export ready`;
    default:
      return event.type;
  }
}

function errorSuffix(error: unknown): string {
  if (error === null || typeof error !== "object") return "";
  const message = (error as { message?: unknown }).message;
  return typeof message === "string" && message ? ` — ${message}` : "";
}
