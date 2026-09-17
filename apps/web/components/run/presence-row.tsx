"use client";

import { useQuery } from "@tanstack/react-query";

import { Avatar } from "@/components/ui/avatar";
import { Tooltip } from "@/components/ui/tooltip";
import { checkIn } from "@/lib/api/runs";
import { cn } from "@/lib/utils";

/**
 * Who else has this console open (PRD §13.4 B).
 *
 * The same request announces us and reads the room, every 10 seconds against a
 * 30-second TTL — two missed beats before an avatar disappears, which is the
 * margin a laptop lid needs. Informational: nothing on this screen behaves
 * differently because someone else is here, it just stops two approvers from
 * deciding the same gate twice.
 */
const HEARTBEAT_MS = 10_000;

export function PresenceRow({ runId, className }: { runId: string; className?: string }) {
  const presence = useQuery({
    queryKey: ["runs", runId, "presence"],
    queryFn: () => checkIn(runId),
    refetchInterval: HEARTBEAT_MS,
    refetchOnWindowFocus: true,
    // A presence blip is never worth an error state on a run console.
    retry: false,
  });

  const viewers = presence.data?.viewers ?? [];
  if (viewers.length <= 1) return null;
  const extra = (presence.data?.total ?? viewers.length) - viewers.length;

  return (
    <div className={cn("flex items-center", className)}>
      <span className="sr-only">
        {viewers.length} people are watching this run: {viewers.map((one) => one.name).join(", ")}
      </span>
      <ul className="flex -space-x-1.5" aria-hidden>
        {viewers.map((viewer) => (
          <li key={viewer.id}>
            <Tooltip content={viewer.name}>
              <span className="inline-flex">
                <Avatar name={viewer.name} size="sm" className="ring-2 ring-[var(--bg)]" />
              </span>
            </Tooltip>
          </li>
        ))}
      </ul>
      {extra > 0 ? (
        <span data-numeric className="ml-2 text-xs text-fg-subtle">
          +{extra}
        </span>
      ) : null}
    </div>
  );
}
