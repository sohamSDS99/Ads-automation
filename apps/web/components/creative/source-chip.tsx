"use client";

import { useState } from "react";

import { EvidenceCard } from "@/components/run/node-evidence";
import { Popover } from "@/components/ui/popover";
import { Skeleton } from "@/components/ui/skeleton";
import type { SourceRef, SourceStage } from "@/lib/api/creative-runs";
import { useEvidence } from "@/lib/queries";

const STAGE: Record<SourceStage, string> = {
  S1: "Research",
  S2: "Campaign plan",
  S3: "Content guidelines",
};

/**
 * `SourceChip` — where one brief line stands (Stage 04 PRD §15.4 D, law 1).
 *
 * `S1 · 1.3.4`: the stage and the node that recorded the fact. It opens the
 * evidence behind it in a popover, fetched only when opened — a brief cites
 * dozens of rows and the reader opens two. A source with no stored row (a
 * plan field, a rule) says what it is instead of showing an empty list.
 */
export function SourceChip({ source, projectId }: { source: SourceRef; projectId: string }) {
  const [open, setOpen] = useState(false);
  const label = `${source.stage} · ${source.node_id}`;
  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      side="top"
      align="end"
      trigger={
        <button
          type="button"
          aria-label={`Source ${label}, ${STAGE[source.stage]}`}
          className="inline-flex shrink-0 items-center rounded-full border px-1.5 py-px align-baseline font-mono text-xs leading-5 text-fg-muted transition-colors hover:border-border-strong hover:text-fg"
        >
          {label}
        </button>
      }
    >
      <SourceDetail source={source} projectId={projectId} open={open} />
    </Popover>
  );
}

/**
 * A chip for figures `calc/` produced rather than a stage recorded — the
 * media plan cites its derived evidence rows, and they open the same way.
 */
export function EvidenceIdsChip({
  label,
  title,
  ids,
  projectId,
}: {
  label: string;
  title: string;
  ids: string[];
  projectId: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      side="top"
      align="end"
      trigger={
        <button
          type="button"
          aria-label={`Source: ${title}`}
          className="inline-flex shrink-0 items-center rounded-full border px-1.5 py-px align-baseline font-mono text-xs leading-5 text-fg-muted transition-colors hover:border-border-strong hover:text-fg"
        >
          {label}
        </button>
      }
    >
      <div className="flex flex-col gap-2">
        <p className="text-xs text-fg-muted">{title}</p>
        <EvidenceRows ids={ids} projectId={projectId} open={open} />
      </div>
    </Popover>
  );
}

function SourceDetail({
  source,
  projectId,
  open,
}: {
  source: SourceRef;
  projectId: string;
  open: boolean;
}) {
  const ids = source.evidence_ids;
  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-fg-muted">
        <span className="font-medium text-fg">{STAGE[source.stage]}</span> · node{" "}
        <span className="font-mono">{source.node_id}</span>
      </p>
      {source.field ? (
        <p className="break-all font-mono text-xs text-fg-muted">{source.field}</p>
      ) : null}
      {source.rule_id ? (
        <p className="text-xs text-fg-muted">
          Rule <span className="font-mono text-fg">{source.rule_id}</span>
        </p>
      ) : null}
      {ids.length === 0 ? (
        <p className="text-xs text-fg-subtle">
          This line stands on the {STAGE[source.stage].toLowerCase()} record itself; it cites no
          stored evidence row.
        </p>
      ) : (
        <EvidenceRows ids={ids} projectId={projectId} open={open} />
      )}
    </div>
  );
}

/** The cited rows, fetched once the popover is open. */
function EvidenceRows({ ids, projectId, open }: { ids: string[]; projectId: string; open: boolean }) {
  const evidence = useEvidence({ project_id: projectId, ids, limit: 50 }, open && ids.length > 0);
  const items = evidence.data?.pages.flatMap((page) => page.items) ?? [];
  if (evidence.isPending) return <Skeleton className="h-20 w-full" />;
  return (
    <div className="flex max-h-72 flex-col gap-2 overflow-y-auto">
      {items.map((item) => (
        <EvidenceCard key={item.id} item={item} projectId={projectId} />
      ))}
      {items.length < ids.length ? (
        <p className="text-xs text-fg-subtle">
          {ids.length - items.length} cited {ids.length - items.length === 1 ? "row is" : "rows are"}{" "}
          no longer stored.
        </p>
      ) : null}
    </div>
  );
}
