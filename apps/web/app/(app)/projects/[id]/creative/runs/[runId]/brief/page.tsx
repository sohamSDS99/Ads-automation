"use client";

import { use } from "react";

import { BriefScreen } from "@/components/creative/brief-screen";

/**
 * `/projects/[id]/creative/runs/[runId]/brief` — the brief and its G7 card
 * (Stage 04 PRD §15.3, §15.4 D). Any role reads it; only whoever the
 * approvals surface says may decide G7 sees a decide control.
 */
export default function CreativeBriefPage({
  params,
}: {
  params: Promise<{ id: string; runId: string }>;
}) {
  const { id, runId } = use(params);
  return <BriefScreen projectId={id} runId={runId} />;
}
