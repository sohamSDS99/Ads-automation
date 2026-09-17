"use client";

import Link from "next/link";

import { Popover } from "@/components/ui/popover";
import { SOURCE_LABEL, evidenceSummary } from "@/lib/api/evidence";
import type { Claim } from "@/lib/api/reports";
import { absoluteTime, relativeTime } from "@/lib/format";
import type { Citations } from "@/lib/citations";
import { cn } from "@/lib/utils";

/**
 * A claim and the evidence under it (PRD §13.4 C).
 *
 * Every citation is a real control: hover or focus opens the row behind it,
 * clicking through lands in the explorer filtered to it. The chip is numbered
 * per claim rather than per document — a reader checking one sentence does not
 * care that it is footnote 214.
 */
export function ClaimLine({
  claim,
  citations,
  projectId,
  className,
}: {
  claim: Claim;
  citations: Citations;
  projectId: string;
  className?: string;
}) {
  return (
    <span className={cn("text-fg", className)}>
      {claim.statement}{" "}
      <CiteGroup ids={claim.evidence_ids} citations={citations} projectId={projectId} />
      {claim.confidence !== "high" ? (
        <span className="ml-1.5 text-xs text-fg-subtle">({claim.confidence} confidence)</span>
      ) : null}
    </span>
  );
}

export function CiteGroup({
  ids,
  citations,
  projectId,
}: {
  ids: string[] | undefined;
  citations: Citations;
  projectId: string;
}) {
  if (!ids || ids.length === 0) return null;
  return (
    <sup className="ml-0.5 inline-flex gap-0.5 align-super">
      {ids.map((id, index) => (
        <Cite key={id} id={id} index={index + 1} citations={citations} projectId={projectId} />
      ))}
    </sup>
  );
}

function Cite({
  id,
  index,
  citations,
  projectId,
}: {
  id: string;
  index: number;
  citations: Citations;
  projectId: string;
}) {
  const item = citations.byId.get(id);
  return (
    <Popover
      side="top"
      trigger={
        <button
          type="button"
          className={cn(
            "rounded-[4px] px-1 text-[0.625rem] leading-4 font-medium transition-colors",
            "bg-accent-soft text-accent hover:bg-accent hover:text-accent-fg",
          )}
        >
          {index}
          <span className="sr-only">Show the evidence behind this</span>
        </button>
      }
    >
      {item ? (
        <div className="space-y-2">
          <p className="flex items-baseline justify-between gap-2">
            <span className="text-xs font-medium text-fg">{SOURCE_LABEL[item.source]}</span>
            <time
              className="text-xs text-fg-subtle"
              dateTime={item.fetched_at}
              title={absoluteTime(item.fetched_at)}
            >
              {relativeTime(item.fetched_at)}
            </time>
          </p>
          <p className="line-clamp-5 text-sm text-fg-muted">{evidenceSummary(item)}</p>
          <p className="flex flex-wrap items-center gap-3">
            <span className="font-mono text-[0.6875rem] text-fg-subtle">{item.kind}</span>
            {item.source_url ? (
              <a
                href={item.source_url}
                target="_blank"
                rel="noreferrer"
                className="text-xs text-accent hover:underline"
              >
                Source
              </a>
            ) : null}
            <Link
              href={`/projects/${projectId}/evidence?ids=${item.id}`}
              className="text-xs text-accent hover:underline"
            >
              Open in evidence
            </Link>
          </p>
        </div>
      ) : citations.loading ? (
        <p className="text-sm text-fg-muted">Loading the evidence…</p>
      ) : (
        <p className="text-sm text-fg-muted">
          This row is no longer stored. The claim was made against it when the run happened.
        </p>
      )}
    </Popover>
  );
}
