"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { use } from "react";
import { useSearchParams } from "next/navigation";

import { PlanCompare } from "@/components/plan/plan-compare";

/**
 * `/projects/[id]/plan/compare?a=&b=` — the version diff (§15.2, §15.3 F).
 *
 * `a` and `b` are plan run ids, which is what the history table carries and
 * what every other Stage 02 route is keyed on. A link with one of them missing
 * renders the picker rather than an error: arriving here from a bookmark with a
 * stale query is not a failure, it is somebody who wants to compare two
 * versions and has not said which yet.
 */
export default function PlanComparePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const search = useSearchParams();

  return (
    <div className="space-y-3">
      <Link
        href={`/projects/${id}/plan`}
        className="inline-flex w-fit items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
      >
        <ArrowLeft className="size-4 shrink-0" aria-hidden />
        <span className="truncate">Campaign planning</span>
      </Link>
      <PlanCompare projectId={id} a={search.get("a")} b={search.get("b")} />
    </div>
  );
}
