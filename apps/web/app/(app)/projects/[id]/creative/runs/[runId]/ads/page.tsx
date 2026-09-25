"use client";

import { use } from "react";

import { AdStudio } from "@/components/creative/ad-studio";

/**
 * `/projects/[id]/creative/runs/[runId]/ads` — the Ad Studio (Stage 04 PRD
 * §15.3, §15.4 E): every Search ad of the run, its SERP preview, headlines,
 * pairs, descriptions and variant B. Any role reads it; only
 * `creative_execute` edits or swaps.
 */
export default function AdStudioPage({ params }: { params: Promise<{ id: string; runId: string }> }) {
  const { id, runId } = use(params);
  return <AdStudio projectId={id} runId={runId} />;
}
