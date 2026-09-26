"use client";

import { use } from "react";

import { PackageDocument } from "@/components/creative/package-document";

/**
 * `/projects/[id]/creative/packages/[packageId]` — the released package's
 * canonical URL (Stage 04 PRD §15.3). Read-only for every role.
 */
export default function ReleasedPackagePage({ params }: { params: Promise<{ id: string; packageId: string }> }) {
  const { id, packageId } = use(params);
  return <PackageDocument projectId={id} packageId={packageId} />;
}
