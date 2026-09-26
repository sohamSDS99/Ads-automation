"use client";

import { use } from "react";

import { CompareScreen } from "@/components/creative/compare-screen";

/**
 * `/projects/[id]/creative/compare?a=&b=` — the package diff (Stage 04 PRD
 * §15.3, §15.4 L): `a` the earlier package, `b` the later.
 */
export default function ComparePackagesPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ a?: string; b?: string }>;
}) {
  const { id } = use(params);
  const { a, b } = use(searchParams);
  return <CompareScreen projectId={id} before={a || null} after={b || null} />;
}
