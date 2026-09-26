"use client";

import { use } from "react";

import { QaScreen } from "@/components/creative/qa-screen";

/**
 * `/projects/[id]/creative/runs/[runId]/qa` — previews, spec conformance,
 * exceptions and launch minimums (Stage 04 PRD §15.3, §15.4 K). Any role.
 */
export default function CreativeQaPage({ params }: { params: Promise<{ id: string; runId: string }> }) {
  const { id, runId } = use(params);
  return <QaScreen projectId={id} runId={runId} />;
}
