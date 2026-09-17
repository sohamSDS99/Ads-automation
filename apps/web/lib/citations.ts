/**
 * Resolving a report's citations.
 *
 * `ResearchReport` carries evidence ids everywhere and the viewer needs the
 * rows behind them all at once — one request per superscript would be hundreds
 * on a full report. `GET /evidence?ids=` takes 200 at a time, so this chunks
 * and merges; the chunks are separate queries so a long report paints its first
 * citations while the rest arrive.
 */
"use client";

import { useQueries } from "@tanstack/react-query";
import { useMemo } from "react";

import { MAX_IDS, listEvidence, type EvidenceItem } from "@/lib/api/evidence";

export type Citations = {
  byId: Map<string, EvidenceItem>;
  loading: boolean;
};

export function useCitations(projectId: string, ids: string[]): Citations {
  const chunks = useMemo(() => chunk(ids, MAX_IDS), [ids]);

  const results = useQueries({
    queries: chunks.map((batch) => ({
      queryKey: ["evidence", { project_id: projectId, ids: batch }],
      queryFn: () => listEvidence({ project_id: projectId, ids: batch, limit: MAX_IDS }),
      staleTime: 5 * 60 * 1000,
    })),
  });

  return useMemo(() => {
    const byId = new Map<string, EvidenceItem>();
    for (const result of results) {
      for (const item of result.data?.items ?? []) byId.set(item.id, item);
    }
    return { byId, loading: results.some((result) => result.isPending) };
  }, [results]);
}

function chunk<T>(items: T[], size: number): T[][] {
  const out: T[][] = [];
  for (let index = 0; index < items.length; index += size) {
    out.push(items.slice(index, index + size));
  }
  return out;
}
