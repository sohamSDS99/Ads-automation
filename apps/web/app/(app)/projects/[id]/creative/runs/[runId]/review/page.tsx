"use client";

import { use } from "react";

import { ReviewScreen } from "@/components/creative/review-screen";

/**
 * `/projects/[id]/creative/runs/[runId]/review` — the G8 / G8b review
 * workspace (Stage 04 PRD §15.3, §15.4 H). Any role looks; only whoever the
 * approvals surface says may decide the gate sees a decide control.
 */
export default function CreativeReviewPage({
  params,
}: {
  params: Promise<{ id: string; runId: string }>;
}) {
  const { id, runId } = use(params);
  return <ReviewScreen projectId={id} runId={runId} />;
}
