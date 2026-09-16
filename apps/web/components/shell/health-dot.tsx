"use client";

import { useQuery } from "@tanstack/react-query";

import { getHealth } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Live API reachability, polled through the same rewrite the rest of the app
 * uses. If this dot is green the browser → web → api private path is working.
 */
export function HealthDot() {
  const { data, isError, isPending } = useQuery({
    queryKey: ["health"],
    queryFn: getHealth,
    refetchInterval: 15_000,
    retry: false,
  });

  const state = isPending ? "pending" : isError || data?.status !== "ok" ? "down" : "up";

  const label =
    state === "pending"
      ? "Checking API…"
      : state === "up"
        ? `API ok · v${data?.version ?? "?"}`
        : data
          ? `API degraded · db ${data.db} · redis ${data.redis}`
          : "API unreachable";

  return (
    <div
      role="status"
      aria-label={label}
      title={label}
      className="flex items-center gap-2 text-xs text-fg-muted"
    >
      <span
        aria-hidden
        className={cn(
          "size-2 rounded-full",
          state === "up" && "bg-status-success",
          state === "down" && "bg-status-failed",
          state === "pending" && "bg-status-skipped",
        )}
      />
      {/* aria-hidden so the label is announced once, by the container. */}
      <span aria-hidden className="hidden font-mono sm:inline">
        {label}
      </span>
    </div>
  );
}
