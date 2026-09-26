"use client";

import { use } from "react";

import { PackageScreen } from "@/components/creative/package-screen";

/**
 * `/projects/[id]/creative/runs/[runId]/package` — the manifest and release
 * (Stage 04 PRD §15.3, §15.4 L). Any role reads it; only `creative_release`
 * sees the release control.
 */
export default function CreativePackagePage({ params }: { params: Promise<{ id: string; runId: string }> }) {
  const { id, runId } = use(params);
  return <PackageScreen projectId={id} runId={runId} />;
}
