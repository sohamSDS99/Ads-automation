"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { use } from "react";

import { SpecSheet } from "@/components/guidelines/spec-sheet";
import { Alert } from "@/components/ui/alert";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { versionLabel } from "@/lib/api/guidelines";
import { useGuideline, useGuidelines, useProject } from "@/lib/queries";

/**
 * `/projects/[id]/guidelines/specs` — the asset spec sheet (PRD §15.2).
 *
 * A page of its own, rather than only a section of the rulebook, because it is
 * the one part of Stage 03 a copywriter opens on its own: *how long can this
 * headline be*. Everything else in the rulebook is read once and referred to;
 * this is looked up.
 *
 * It reads the published version when there is one and falls back to the
 * newest draft, and says which it is showing — a limit from an unpublished
 * draft is a limit nobody has signed off, and quoting it as settled is how a
 * campaign gets built against a rule that later changes.
 */
export default function SpecSheetPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const project = useProject(id);
  const guidelines = useGuidelines(id);

  const versions = guidelines.data?.versions ?? [];
  const published = versions.find((version) => version.status === "published");
  const row = published ?? versions.find((version) => version.status !== "superseded");
  const detail = useGuideline(row?.id ?? null);
  const specs = detail.data?.payload?.asset_specs;

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-4">
      <Link
        href={`/projects/${id}/guidelines`}
        className="inline-flex w-fit items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
      >
        <ArrowLeft className="size-4 shrink-0" aria-hidden />
        <span className="truncate">{project.data?.name ?? "Content guidelines"}</span>
      </Link>

      <header className="min-w-0">
        <h1 className="text-[length:var(--text-xl)] font-semibold tracking-tight">Asset specs</h1>
        <p className="mt-1 max-w-prose text-sm text-fg-muted">
          What Google accepts, by campaign type and asset type, with the constant each number came
          from and the date anybody last checked it.
        </p>
      </header>

      {row && !published ? (
        <Alert tone="warning" title="These come from a draft">
          {versionLabel(row) === "—" ? "This version" : versionLabel(row)} has not been published,
          so nobody has signed these off. The published rulebook is the one Stage 04 enforces.
        </Alert>
      ) : null}

      <Card>
        <CardHeader
          title={
            published
              ? `Published ${versionLabel(published)}`
              : row
                ? "Draft"
                : "No guideline version yet"
          }
        />
        <CardBody>
          {guidelines.isPending || (row && detail.isPending) ? (
            <Skeleton className="h-64 w-full" />
          ) : (
            <SpecSheet sheet={specs?.sheet} scope={specs?.scope} />
          )}
        </CardBody>
      </Card>
    </div>
  );
}
