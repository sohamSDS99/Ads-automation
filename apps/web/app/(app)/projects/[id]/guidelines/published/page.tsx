"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { use } from "react";

import { Rulebook } from "@/components/guidelines/rulebook";
import { useProject } from "@/lib/queries";

/**
 * `/projects/[id]/guidelines/published` — the living rulebook (PRD §15.2).
 *
 * The canonical URL: the one people bookmark, paste into a brief, and open to
 * settle an argument about what we are allowed to say. It resolves to whatever
 * is published *now*, which is why it is a route of its own rather than a link
 * to a version id — a bookmark that keeps pointing at last quarter's rules is
 * worse than no bookmark.
 */
export default function PublishedRulebookPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const project = useProject(id);

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-4">
      <Link
        href={`/projects/${id}/guidelines`}
        className="inline-flex w-fit items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
      >
        <ArrowLeft className="size-4 shrink-0" aria-hidden />
        <span className="truncate">{project.data?.name ?? "Content guidelines"}</span>
      </Link>
      <Rulebook projectId={id} mode="published" />
    </div>
  );
}
