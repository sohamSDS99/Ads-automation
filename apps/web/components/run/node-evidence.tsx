"use client";

import { ExternalLink, FileSearch } from "lucide-react";
import Link from "next/link";

import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { SOURCE_LABEL, evidenceSummary, type EvidenceItem } from "@/lib/api/evidence";
import { absoluteTime, relativeTime } from "@/lib/format";
import { useEvidence } from "@/lib/queries";

/**
 * What a node actually read (PRD §13.4 B, Evidence tab).
 *
 * The node stores ids; the rows behind them are fetched in one request. A card
 * shows where the fact came from and when it was fetched, because "the model
 * said so" is the failure this whole product is arranged to prevent.
 */
export function NodeEvidence({ projectId, ids }: { projectId: string; ids: string[] }) {
  const query = useEvidence({ project_id: projectId, ids, limit: 200 }, ids.length > 0);
  const items = query.data?.pages.flatMap((page) => page.items) ?? [];

  if (ids.length === 0) {
    return (
      <EmptyState
        icon={FileSearch}
        title="No evidence cited"
        description="This node computed its answer from other nodes' output rather than from a stored fact."
      />
    );
  }

  if (query.isPending) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-20 w-full" />
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {items.map((item) => (
        <EvidenceCard key={item.id} item={item} projectId={projectId} />
      ))}
      {items.length < ids.length ? (
        <p className="px-1 text-xs text-fg-subtle">
          {ids.length - items.length} cited {ids.length - items.length === 1 ? "row is" : "rows are"}{" "}
          no longer stored.
        </p>
      ) : null}
    </div>
  );
}

export function EvidenceCard({ item, projectId }: { item: EvidenceItem; projectId?: string }) {
  const href = projectId
    ? `/projects/${projectId}/evidence?ids=${item.id}`
    : `/evidence?ids=${item.id}`;
  return (
    <article className="rounded-[var(--radius)] border bg-surface px-3 py-2.5">
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-xs font-medium text-fg">{SOURCE_LABEL[item.source]}</span>
        <time
          className="text-xs text-fg-subtle"
          dateTime={item.fetched_at}
          title={absoluteTime(item.fetched_at)}
        >
          {relativeTime(item.fetched_at)}
        </time>
      </header>
      <p className="mt-1 line-clamp-3 text-sm text-fg-muted">{evidenceSummary(item)}</p>
      <footer className="mt-2 flex flex-wrap items-center gap-3">
        <span className="font-mono text-[0.6875rem] text-fg-subtle">{item.kind}</span>
        {item.source_url ? (
          <a
            href={item.source_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-xs text-accent hover:underline"
          >
            Source
            <ExternalLink className="size-3" aria-hidden />
          </a>
        ) : null}
        <Link href={href} className="text-xs text-accent hover:underline">
          Open in evidence
        </Link>
      </footer>
    </article>
  );
}
