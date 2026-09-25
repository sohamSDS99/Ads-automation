"use client";

import { use } from "react";

import { LandingAuditScreen } from "@/components/creative/landing-audit-screen";

/**
 * `/projects/[id]/creative/runs/[runId]/landing` — the Landing audit (Stage 04
 * PRD §15.3, §15.4 J): one card per landing URL 4.5.1/4.5.2 audited, with the
 * fold on both devices, the H1 against the ad, the form and the patch for the
 * site owner. Any role reads it; Stage 04 deploys nothing (law 41).
 */
export default function LandingAuditPage({ params }: { params: Promise<{ id: string; runId: string }> }) {
  const { id, runId } = use(params);
  return <LandingAuditScreen projectId={id} runId={runId} />;
}
