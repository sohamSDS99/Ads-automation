"use client";

import { ArrowLeft, GitCompareArrows, Lock, PackageOpen } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { LaunchMinimums } from "@/components/creative/launch-minimums";
import { MonoId } from "@/components/creative/mono-id";
import { PackageContents } from "@/components/creative/package-contents";
import { PACKAGE_STATUS, SupersededBanners } from "@/components/creative/package-screen";
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
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api";
import { compareHref, packageHref } from "@/lib/api/creative-packages";
import { absoluteTime, shortDate } from "@/lib/format";
import { useCreativeOverview, usePackage } from "@/lib/queries";

/**
 * `/projects/[id]/creative/packages/[packageId]` — **the canonical URL of a
 * released package** (Stage 04 PRD §15.3). One URL per package, forever: the
 * id never changes and a released package never does (law 42), so this page
 * is the record Stage 05, a reviewer and an auditor all read.
 *
 * Read-only for every role, the release control included — releasing happens
 * on the run's Package screen. It carries a `rel=canonical` link to itself.
 */
export function PackageDocument({ projectId, packageId }: { projectId: string; packageId: string }) {
  const view = usePackage(packageId);
  const overview = useCreativeOverview(projectId);
  const router = useRouter();
  const self = packageHref(projectId, packageId);
  const back = (
    <Link
      href={`/projects/${projectId}/creative`}
      className="inline-flex w-fit items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
    >
      <ArrowLeft className="size-4 shrink-0" aria-hidden />
      Back to Copy &amp; creative
    </Link>
  );

  if (view.isPending) {
    return (
      <div className="flex flex-col gap-6">
        {back}
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-96 w-full" />
      </div>
    );
  }
  if (view.error) {
    return (
      <div className="flex flex-col gap-6">
        {back}
        {view.error instanceof ApiError && view.error.status === 404 ? (
          <EmptyState
            icon={PackageOpen}
            title="No such package"
            description="This package does not exist in this workspace. Released packages are listed on the Copy & creative page."
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
  const isRecord = pkg.status === "released" || pkg.status === "superseded";
  const others = (overview.data?.packages ?? []).filter((p) => p.package_id !== pkg.package_id);
  const current = others.find((p) => p.status === "released") ?? null;

  return (
    <article className="flex flex-col gap-8" data-testid="package-document" data-status={pkg.status}>
      {/* React hoists this into <head>: the id-based URL is the one to cite. */}
      <link rel="canonical" href={self} />
      <div className="flex flex-col gap-4">
        {back}
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex min-w-0 flex-col gap-1">
            <h1 className="flex flex-wrap items-center gap-3 text-lg font-medium tracking-tight text-fg">
              {isRecord ? `Package v${data.row_version}` : "Unreleased package"}
              <Badge tone={status.tone} data-testid="package-status" data-status={pkg.status}>
                {isRecord ? <Lock className="size-3" aria-hidden /> : null}
                {status.label}
              </Badge>
            </h1>
            <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-fg-muted tabular-nums">
              {data.released_at ? (
                <>
                  Released by {data.released_by_name ?? "someone"} on{" "}
                  <time dateTime={data.released_at} title={absoluteTime(data.released_at)}>
                    {absoluteTime(data.released_at)}
                  </time>{" "}
                  · hash <MonoId value={data.package_hash ?? pkg.package_hash} label="package hash" />
                </>
              ) : (
                <>Package {pkg.package_id.slice(0, 8)} of run {pkg.creative_run_id.slice(0, 8)}</>
              )}
            </p>
          </div>
          {others.length > 0 ? (
            <label className="flex items-center gap-2 text-sm text-fg-muted">
              <GitCompareArrows className="size-4" aria-hidden />
              <span>Compare with</span>
              <Select
                value=""
                onChange={(event) => {
                  if (event.target.value) router.push(compareHref(projectId, event.target.value, pkg.package_id));
                }}
                aria-label="Compare this package with another version"
                data-testid="compare-with"
              >
                <option value="">Choose a version</option>
                {others.map((p) => (
                  <option key={p.package_id} value={p.package_id}>
                    {p.version > 0 ? `v${p.version}` : "Unreleased"} · {PACKAGE_STATUS[p.status].label.toLowerCase()} ·{" "}
                    {shortDate(p.released_at)}
                  </option>
                ))}
              </Select>
            </label>
          ) : null}
        </div>

        {isRecord ? (
          <p className="flex items-center gap-2 text-sm text-fg" data-testid="immutable-note">
            <Lock className="size-4 shrink-0 text-fg-subtle" aria-hidden />
            Read-only. A released package is immutable: its assets are frozen and its files hashed.
            {pkg.status === "released" ? " This is the version Stage 05 loads." : null}
          </p>
        ) : (
          <Alert tone="info" title="Not released">
            This package has not been released, so it is not a record yet. It is released from its run’s{" "}
            <Link href={`/projects/${projectId}/creative/runs/${pkg.creative_run_id}/package`} className="text-accent hover:underline">
              Package screen
            </Link>
            .
          </Alert>
        )}
        {pkg.status === "superseded" && current ? (
          <Alert tone="info" title={`Superseded by v${current.version}`}>
            Stage 05 now loads{" "}
            <Link href={packageHref(projectId, current.package_id)} className="text-accent hover:underline">
              v{current.version}
            </Link>
            .{" "}
            <Link href={compareHref(projectId, pkg.package_id, current.package_id)} className="text-accent hover:underline">
              See what changed
            </Link>
            .
          </Alert>
        ) : null}
        <SupersededBanners view={data} />
      </div>

      <PackageSection id="package-contents" title="What it ships">
        <PackageContents campaigns={pkg.campaigns} />
      </PackageSection>

      <PackageSection id="package-stops" title="Stops">
        <StopsTable stops={data.release.stops} />
      </PackageSection>

      <PackageSection id="package-checklist" title="Blocking checklist, as 4.7.2 ran it">
        <BlockingChecklist checklist={data.checklist} critique={data.critique} />
      </PackageSection>

      <PackageSection id="package-minimums" title="Launch minimums">
        <LaunchMinimums campaigns={pkg.campaigns} />
      </PackageSection>

      <PackageSection id="package-dependencies" title="Open dependencies">
        <DependenciesTable dependencies={pkg.open_dependencies} />
      </PackageSection>

      <PackageSection id="package-lint" title="Lint summary">
        <LintSummaryTable summary={pkg.lint_summary} />
      </PackageSection>

      <PackageSection id="package-cost-section" title="Cost">
        <CostTable cost={pkg.cost} />
      </PackageSection>

      <PackageSection id="package-pins-section" title="Pins">
        <PinsList pins={pkg.pins} />
      </PackageSection>

      <PackageSection id="package-manifest" title="Manifest">
        <PackageManifest manifest={pkg.manifest} packageId={pkg.package_id} released={isRecord} />
      </PackageSection>
    </article>
  );
}
