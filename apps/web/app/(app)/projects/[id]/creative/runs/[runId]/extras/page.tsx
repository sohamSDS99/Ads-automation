"use client";

import { use } from "react";

import { ExtrasScreen } from "@/components/creative/extras-screen";

/**
 * `/projects/[id]/creative/runs/[runId]/extras` — the Extras screen (Stage 04
 * PRD §15.3, §15.4 F): sitelinks, callouts, snippets, offer-bound promotions
 * and prices, and the lead form with its field trade-off. Any role reads it;
 * nothing on it edits, and offer figures are never editable (law 35).
 *
 * The only route that draws the trade-off chart, so the only one that loads
 * recharts' code on its way to Stage 04 (the per-route split does the rest).
 */
export default function ExtrasPage({ params }: { params: Promise<{ id: string; runId: string }> }) {
  const { id, runId } = use(params);
  return <ExtrasScreen projectId={id} runId={runId} />;
}
