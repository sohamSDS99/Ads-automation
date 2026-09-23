"use client";

import { BookCheck, FileText } from "lucide-react";
import { useState } from "react";

import { PublishDialog } from "@/components/guidelines/publish-dialog";
import { SECTIONS, RulebookSections } from "@/components/guidelines/rulebook-sections";
import {
  ExportControl,
  GUIDELINE_FORMAT_LABEL,
  type GuidelineExportFormat,
} from "@/components/report/export-button";
import { Toc } from "@/components/report/toc";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Can } from "@/lib/session";
import { exportGuideline, versionLabel, type GuidelineDetail } from "@/lib/api/guidelines";
import { absoluteTime, relativeTime } from "@/lib/format";
import { keys, useGuideline, useGuidelines, usePublishedRuleSet } from "@/lib/queries";

const FORMATS = ["pdf", "docx", "md", "json", "xlsx", "ruleset_json"] as const satisfies readonly [
  GuidelineExportFormat,
  ...GuidelineExportFormat[],
];

/**
 * The Rulebook Viewer (PRD §15.3 E).
 *
 * One component, two routes. `/published` is the URL people bookmark and
 * `/runs/[runId]/rulebook` is the draft as it is being built — the same
 * document at two points in its life, so it is the same reader with a
 * different source and a different header.
 *
 * The draft is readable at every point in the run. That is not a nicety: it is
 * how somebody answering G5 sees what they are being asked to confirm. So
 * every section renders a *named* absence when the node that writes it has not
 * run, and nothing here assumes a finished payload.
 */
export function Rulebook({
  projectId,
  /** The draft view resolves the guideline from its run. */
  runId,
  /** The published view takes the project's current published version. */
  mode,
}: {
  projectId: string;
  runId?: string;
  mode: "draft" | "published";
}) {
  const guidelines = useGuidelines(projectId);
  const versions = guidelines.data?.versions ?? [];
  const row =
    mode === "draft"
      ? versions.find((version) => version.guideline_run_id === runId)
      : versions.find((version) => version.status === "published");
  const detail = useGuideline(row?.id ?? null);
  const ruleset = usePublishedRuleSet(projectId);
  const [publishing, setPublishing] = useState(false);

  if (guidelines.isPending || (row && detail.isPending)) {
    return (
      <div className="flex flex-col gap-4">
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-96 w-full" />
      </div>
    );
  }

  if (!row) {
    return mode === "published" ? (
      <EmptyState
        icon={BookCheck}
        title="Nothing published yet"
        description="Until a version is published, Stage 04 has no ruleset to lint against and cannot produce creative. Start a run from the Content guidelines page and publish it once the gates are done."
      />
    ) : (
      <EmptyState
        icon={FileText}
        title="This run has no rulebook row"
        description="A guideline row is created when the run starts. If this run predates that, its console still shows what it produced."
      />
    );
  }

  if (!detail.data) {
    return <Alert tone="error" title="The rulebook could not be loaded">{detail.error?.message}</Alert>;
  }

  const guideline = detail.data;
  const payload = guideline.payload;

  return (
    <div className="flex flex-col gap-6">
      <RulebookHeader
        guideline={guideline}
        rulesetVersion={
          mode === "published" && ruleset.isSuccess ? ruleset.data.ruleset_version : null
        }
        onPublish={() => setPublishing(true)}
      />

      {payload ? (
        <div className="flex gap-8">
          {/* Sticky, and hidden below `lg` rather than collapsed into an
              accordion: on a phone the document is one column and a floating
              index is a second thing covering it. */}
          <Toc
            entries={SECTIONS}
            label="Rulebook contents"
            className="sticky top-4 hidden h-fit w-48 shrink-0 lg:block"
          />
          <div className="min-w-0 flex-1">
            <RulebookSections payload={payload} />
          </div>
        </div>
      ) : (
        <EmptyState
          icon={FileText}
          title="The rulebook has not been written yet"
          description="Node 3.6.1 assembles it from every section once the stages before it have run. The console shows where this run has got to."
        />
      )}

      <PublishDialog
        guideline={guideline}
        projectId={projectId}
        open={publishing}
        onOpenChange={setPublishing}
      />
    </div>
  );
}

/**
 * The pinned header: what this version is, and the two things you can do to it.
 *
 * `unbound_inputs` is stated here rather than buried in the summary, for §4.3
 * rule 2's reason: a rulebook built without legal guardrails must not read as
 * authoritative as one built with them.
 */
function RulebookHeader({
  guideline,
  rulesetVersion,
  onPublish,
}: {
  guideline: GuidelineDetail;
  rulesetVersion: string | null;
  onPublish: () => void;
}) {
  const label = versionLabel(guideline);
  const publishable = guideline.status === "ready_to_publish";

  return (
    <header className="sticky top-0 z-10 flex flex-wrap items-start justify-between gap-4 border-b bg-bg/95 pb-4 backdrop-blur">
      <div className="min-w-0 space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-[length:var(--text-xl)] font-semibold tracking-tight">
            Content rulebook {label === "—" ? "" : label}
          </h1>
          <Badge tone={guideline.status === "published" ? "accent" : "neutral"}>
            {guideline.status.replace(/_/g, " ")}
          </Badge>
          {guideline.signature_stale ? <Badge tone="warning">Signature stale</Badge> : null}
        </div>

        <dl className="flex flex-wrap items-baseline gap-x-6 gap-y-1 text-sm text-fg-muted">
          {rulesetVersion ? (
            <div className="flex items-baseline gap-2">
              <dt>Ruleset</dt>
              <dd data-numeric className="font-mono text-xs text-fg">
                {rulesetVersion}
              </dd>
            </div>
          ) : null}
          <div className="flex items-baseline gap-2">
            <dt>Scope</dt>
            <dd className="text-fg">
              {guideline.mode === "standalone" ? "unscoped" : guideline.mode.replace(/_/g, " ")}
            </dd>
          </div>
          {guideline.published_at ? (
            <div className="flex items-baseline gap-2">
              <dt>Published</dt>
              <dd className="text-fg">
                <time
                  dateTime={guideline.published_at}
                  title={absoluteTime(guideline.published_at)}
                >
                  {relativeTime(guideline.published_at)}
                </time>
                {guideline.published_by_name ? ` by ${guideline.published_by_name}` : ""}
              </dd>
            </div>
          ) : null}
        </dl>

        {guideline.unbound_inputs.length > 0 ? (
          <p className="max-w-prose text-sm text-fg-muted">
            Built without: {guideline.unbound_inputs.join(", ")}. Rules derived from those inputs
            are absent, not relaxed.
          </p>
        ) : null}
      </div>

      <div className="flex shrink-0 items-center gap-2">
        {/* Absent, not disabled, for anyone without the permission — and
            absent for everyone until the rulebook is actually publishable, so
            the control cannot be the thing that teaches somebody the gates are
            outstanding. That is the dialog's job, and the landing's. */}
        {publishable ? (
          <Can permission="guideline_publish">
            <Button onClick={onPublish}>Publish {label === "—" ? "" : label}</Button>
          </Can>
        ) : null}
        <ExportControl
          formats={FORMATS}
          label={(format) => GUIDELINE_FORMAT_LABEL[format]}
          request={(format) =>
            exportGuideline(guideline.id, format).then((accepted) => accepted.job_id)
          }
          invalidate={keys.guideline(guideline.id)}
        />
      </div>
    </header>
  );
}
