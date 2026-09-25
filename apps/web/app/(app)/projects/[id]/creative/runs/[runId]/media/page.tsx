"use client";

import { use } from "react";

import { MediaLibrary } from "@/components/creative/media-library";

/**
 * `/projects/[id]/creative/runs/[runId]/media` — the Media Library (Stage 04
 * PRD §15.3, §15.4 G): concepts, renditions, video and jobs, with the detail
 * drawer and regeneration. Any role reads it; only `creative_execute`
 * regenerates.
 */
export default function MediaLibraryPage({ params }: { params: Promise<{ id: string; runId: string }> }) {
  const { id, runId } = use(params);
  return <MediaLibrary projectId={id} runId={runId} />;
}
