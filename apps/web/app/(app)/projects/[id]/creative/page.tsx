"use client";

import { use } from "react";

import { CreativeLanding } from "@/components/creative/landing";

/** `/projects/[id]/creative` — the Stage 04 landing (PRD §15.3, §15.4 A). Any role. */
export default function CreativeLandingPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  return <CreativeLanding projectId={id} />;
}
