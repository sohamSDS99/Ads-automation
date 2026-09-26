"use client";

import { ArrowLeft, ArrowUpRight, ListChecks, Lock, PackageOpen } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { LaunchMinimums } from "@/components/creative/launch-minimums";
import { MonoId } from "@/components/creative/mono-id";
import {
  BlockingChecklist,
  CostTable,
  DependenciesTable,
  LintSummaryTable,
  PackageManifest,
  PackageSection,
  PinsList,
  StopsTable,
} from "@/components/creative/package-sections";
import { ReleaseDialog } from "@/components/creative/release-dialog";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api";
import { packageHref, type PackageStatus, type PackageView } from "@/lib/api/creative-packages";
import { absoluteTime, relativeTime } from "@/lib/format";
import { useCreativeOverview, useRunPackage } from "@/lib/queries";
import { useSession } from "@/lib/session";

export const PACKAGE_STATUS: Record<PackageStatus, { label: string; tone: "neutral" | "accent" | "warning" | "danger" }> = {
  draft: { label: "Draft", tone: "neutral" },
  blocked: { label: "Blocked", tone: "danger" },
  ready_to_release: { label: "Ready to release", tone: "accent" },
  released: { label: "Released", tone: "accent" },
  superseded: { label: "Superseded", tone: "neutral" },
};

/** The plan or ruleset moved on after the package was built — said before anything else. */
export function SupersededBanners({ view }: { view: PackageView }) {
  return (
    <>
      {view.plan_superseded ? (
        <Alert tone="warning" title="The plan behind this package was superseded">
          Package v{view.row_version || "0"} was written for plan v{view.package.pins.plan_version}. Release refuses a
          package whose plan was superseded; start a new creative run on the current plan.
        </Alert>
      ) : null}
      {view.ruleset_superseded ? (
        <Alert tone="info" title="A newer ruleset has been published">
          This package was checked against ruleset <span className="font-mono text-xs">{view.package.pins.ruleset_version}</span>.
          A newer one does not re-pin it (law 42); a new run checks against the newer one.
        </Alert>
      ) : null}
    </>
  );
}

/**
 * The Package screen (Stage 04 PRD §15.3 `…/runs/[runId]/package`, §15.4 L):
 * 4.7.2's thirteen blocking checks, the stops, launch minimums, open
 * dependencies with `Blocks launch` chips, the lint summary, cost estimate
 * against actual, and the manifest — then release.
 *
 * All of it is the server's `PackageView`. The checklist is rendered, never
 * re-derived; whether the package is releasable and the version it would mint
 * are the server's answers. The release control is **absent** for anyone
 * without `creative_release` (§15.2, law 40's "decide controls are absent").
 */
export function PackageScreen({ projectId, runId }: { projectId: string; runId: string }) {
  const view = useRunPackage(runId);
  const overview = useCreativeOverview(projectId);
  const { has } = useSession();
  const [releasing, setReleasing] = useState(false);
  const home = `/projects/${projectId}/creative/runs/${runId}`;

  const back = (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <Link
        href={home}
        className="inline-flex w-fit items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
      >
        <ArrowLeft className="size-4 shrink-0" aria-hidden />
        Back to the Creative Console
      </Link>
      <Link href={`${home}/qa`} className="inline-flex items-center gap-1.5 text-sm text-accent hover:underline">
        <ListChecks className="size-4" aria-hidden />
        QA
      </Link>
    </div>
  );

  if (view.isPending) {
    return (
      <div className="flex flex-col gap-6">
        {back}
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-72 w-full" />
      </div>
    );
  }
  if (view.error) {
    const missing = view.error instanceof ApiError && view.error.status === 404;
    return (
      <div className="flex flex-col gap-6">
        {back}
        {missing ? (
          <EmptyState
            icon={PackageOpen}
            title="No package yet"
            description={view.error.message}
            action={
              <Link href={home} className="text-sm text-accent hover:underline">
                Follow the run in the console
              </Link>
            }
          />
        ) : (
          <Alert tone="error" title="The package could not be loaded">
            {view.error instanceof ApiError ? view.error.message : "Refresh the page to try again."}
          </Alert>
        )}
      </div>
    );
  }

  const data = view.data;
  const pkg = data.package;
  const status = PACKAGE_STATUS[pkg.status];
  const released = data.release.version_to_mint === null;
  const canRelease = has("creative_release") && data.release.releasable && data.release.version_to_mint !== null;
  const current = overview.data?.packages.find((p) => p.status === "released" && p.package_id !== pkg.package_id) ?? null;

  return (
    <div className="flex flex-col gap-8">
      <div className="flex flex-col gap-4">
        {back}
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex min-w-0 flex-col gap-1">
            <h1 className="flex flex-wrap items-center gap-3 text-lg font-medium tracking-tight text-fg">
              {released ? `Package v${data.row_version}` : "Package"}
              <Badge tone={status.tone} data-testid="package-status" data-status={pkg.status}>
                {status.label}
              </Badge>
            </h1>
            <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-fg-muted tabular-nums">
              {released && data.released_at ? (
                <>
                  Released by {data.released_by_name ?? "someone"}{" "}
                  <time dateTime={data.released_at} title={absoluteTime(data.released_at)}>
                    {relativeTime(data.released_at)}
                  </time>
                  · hash <MonoId value={data.package_hash ?? pkg.package_hash} label="package hash" />
                </>
              ) : (
                <>
                  Assembled{" "}
                  <time dateTime={data.created_at} title={absoluteTime(data.created_at)}>
                    {relativeTime(data.created_at)}
                  </time>
                  {data.release.version_to_mint !== null ? ` · releases as v${data.release.version_to_mint}` : ""}
                </>
              )}
            </p>
          </div>
          {released ? (
            <Link
              href={packageHref(projectId, pkg.package_id)}
              className="inline-flex items-center gap-1.5 text-sm text-accent hover:underline"
              data-testid="open-released"
            >
              Open the released v{data.row_version}
              <ArrowUpRight className="size-4" aria-hidden />
            </Link>
          ) : canRelease ? (
            <Button onClick={() => setReleasing(true)} data-testid="release-open">
              <Lock aria-hidden />
              Release v{data.release.version_to_mint}
            </Button>
          ) : null}
        </div>
        <SupersededBanners view={data} />
        {!released && data.release.reason ? (
          <Alert tone={pkg.status === "blocked" ? "error" : "info"} title="Not releasable yet">
            <span data-testid="release-reason">{data.release.reason}</span>
          </Alert>
        ) : null}
      </div>

      <PackageSection
        id="package-checklist"
        title="Blocking checklist"
        meta={
          data.critique ? (
            <span className="text-sm text-fg-muted tabular-nums">
              {data.checklist.filter((c) => c.passed).length} of {data.checklist.length} passed
            </span>
          ) : null
        }
      >
        <BlockingChecklist checklist={data.checklist} critique={data.critique} />
      </PackageSection>

      <PackageSection id="package-stops" title="Stops">
        <StopsTable stops={data.release.stops} />
      </PackageSection>

      <PackageSection id="package-minimums" title="Launch minimums">
        <LaunchMinimums campaigns={pkg.campaigns} />
      </PackageSection>

      <PackageSection
        id="package-dependencies"
        title="Open dependencies"
        meta={
          <span className="text-sm text-fg-muted tabular-nums">
            {pkg.open_dependencies.filter((d) => d.blocking_for === "launch").length} block launch
          </span>
        }
      >
        <DependenciesTable dependencies={pkg.open_dependencies} />
      </PackageSection>

      <PackageSection
        id="package-lint"
        title="Lint summary"
        meta={<span className="font-mono text-xs text-fg-muted">{pkg.lint_summary.ruleset_version}</span>}
      >
        <LintSummaryTable summary={pkg.lint_summary} />
      </PackageSection>

      <PackageSection id="package-cost-section" title="Cost">
        <CostTable cost={pkg.cost} />
      </PackageSection>

      <PackageSection id="package-pins-section" title="Pins">
        <PinsList pins={pkg.pins} />
      </PackageSection>

      <PackageSection id="package-manifest" title="Manifest">
        <PackageManifest manifest={pkg.manifest} packageId={pkg.package_id} released={released} />
      </PackageSection>

      {canRelease ? (
        <ReleaseDialog
          view={data}
          projectId={projectId}
          runId={runId}
          released={current}
          open={releasing}
          onOpenChange={setReleasing}
        />
      ) : null}
    </div>
  );
}
