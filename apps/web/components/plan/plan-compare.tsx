"use client";

/**
 * Two plan versions, side by side (Stage 02 PRD §15.3 F).
 *
 * The question is never "what does this plan say" — the Plan Viewer answers
 * that — it is "what am I being asked to re-approve". So the page leads with
 * the scalars that decide something (the envelope above all, with the delta)
 * and then lists what changed section by section, including the tree as three
 * levels rather than one nested blob.
 *
 * The renderer is `components/ui/diff.tsx`, the same one the Stage 01 run
 * comparison uses. What is local to this screen is the picker and the header:
 * plans are argued about by **version**, not by date, and a reader who has to
 * translate "the run from three days ago" into "version 2" has been given the
 * wrong noun.
 *
 * Newer on the right, older on the left, always — and the page says so. A diff
 * that silently flips direction when the reader picks the versions in the other
 * order turns every "+$12,000" into a question about which way round it is.
 */

import { ArrowLeftRight } from "lucide-react";
import Link from "next/link";
import { useMemo } from "react";
import { useRouter } from "next/navigation";

import { Alert } from "@/components/ui/alert";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { DiffBody } from "@/components/ui/diff";
import { EmptyState } from "@/components/ui/empty-state";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import type { PlanVersion } from "@/lib/api/plan";
import { absoluteTime, relativeTime, shortDate } from "@/lib/format";
import { usePlanDiff, usePlans } from "@/lib/queries";

export function PlanCompare({
  projectId,
  a,
  b,
}: {
  projectId: string;
  a: string | null;
  b: string | null;
}) {
  const plans = usePlans(projectId);
  const router = useRouter();
  const versions = useMemo(() => plans.data?.items ?? [], [plans.data]);

  // Newest first from the API, so the default pair is "the last two", which is
  // the comparison anyone arriving without a query wanted.
  const left = a ?? versions[1]?.plan_run_id ?? null;
  const right = b ?? versions[0]?.plan_run_id ?? null;

  const diff = usePlanDiff(right, left);

  if (plans.isPending) return <Skeleton className="h-64 w-full" />;

  if (versions.length < 2) {
    return (
      <EmptyState
        icon={ArrowLeftRight}
        title="Nothing to compare yet"
        description="A comparison needs two plan versions in this project. Run the planner again after a change and this page will show what moved."
      />
    );
  }

  function pick(side: "a" | "b", planRunId: string) {
    const next = new URLSearchParams({ a: left ?? "", b: right ?? "" });
    next.set(side, planRunId);
    router.replace(`/projects/${projectId}/plan/compare?${next}`);
  }

  const leftPlan = versions.find((item) => item.plan_run_id === left);
  const rightPlan = versions.find((item) => item.plan_run_id === right);

  return (
    <Card>
      <CardHeader
        title="What changed between two versions"
        description={
          <>
            Comparing {describe(rightPlan)} with {describe(leftPlan)}. Everything below reads{" "}
            <span className="text-fg">older → newer</span>.
          </>
        }
        actions={
          <div className="flex flex-wrap items-end gap-2">
            <VersionPicker
              label="Older"
              value={left}
              versions={versions}
              exclude={right}
              onChange={(value) => pick("a", value)}
            />
            <VersionPicker
              label="Newer"
              value={right}
              versions={versions}
              exclude={left}
              onChange={(value) => pick("b", value)}
            />
          </div>
        }
      />
      <CardBody className="space-y-4">
        {left === right ? (
          <Alert tone="warning" title="Those are the same version">
            Pick two different versions to see what moved between them.
          </Alert>
        ) : diff.isPending ? (
          <Skeleton className="h-40 w-full" />
        ) : diff.isError ? (
          <Alert tone="warning" title="Nothing to compare">
            {diff.error instanceof Error
              ? diff.error.message
              : "One of those versions has no plan payload to compare."}
          </Alert>
        ) : diff.data ? (
          <>
            <p className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-fg-muted">
              <Link
                href={`/projects/${projectId}/plan/runs/${diff.data.against_plan_run_id}/plan`}
                className="text-accent hover:underline"
              >
                Open {versionLabel(diff.data.against_version)}
              </Link>
              <Link
                href={`/projects/${projectId}/plan/runs/${diff.data.plan_run_id}/plan`}
                className="text-accent hover:underline"
              >
                Open {versionLabel(diff.data.version)}
              </Link>
            </p>
            <DiffBody
              scalars={diff.data.scalars}
              sections={diff.data.sections}
              unchanged={diff.data.unchanged}
              unchangedNote="Nothing the plan contract tracks differs between these two versions. Evidence ids are not compared — they are re-minted on every run, so every one of them is new by definition."
            />
          </>
        ) : null}
      </CardBody>
    </Card>
  );
}

function VersionPicker({
  label,
  value,
  versions,
  exclude,
  onChange,
}: {
  label: string;
  value: string | null;
  versions: PlanVersion[];
  exclude: string | null;
  onChange: (planRunId: string) => void;
}) {
  return (
    <label className="flex flex-col gap-1 text-xs text-fg-muted">
      {label}
      <Select value={value ?? ""} onChange={(event) => onChange(event.target.value)}>
        {versions.map((item) => (
          <option key={item.plan_run_id} value={item.plan_run_id} disabled={item.plan_run_id === exclude}>
            {optionLabel(item)}
          </option>
        ))}
      </Select>
    </label>
  );
}

/**
 * How a version reads in a picker.
 *
 * An unfrozen plan has no version — every one of them sits at 0 — so it is
 * named by the day it was built instead. Printing "v0" three times would offer
 * the reader three identical options.
 */
function optionLabel(item: PlanVersion): string {
  const name = item.version > 0 ? `v${item.version}` : `draft ${shortDate(item.created_at)}`;
  return `${name} · ${item.status.replace(/_/g, " ")}`;
}

function versionLabel(version: number): string {
  return version > 0 ? `v${version}` : "the unversioned plan";
}

function describe(item: PlanVersion | undefined): React.ReactNode {
  if (!item) return "a version that is no longer listed";
  const stamp = item.frozen_at ?? item.created_at;
  return (
    <>
      <span className="text-fg">{item.version > 0 ? `v${item.version}` : "the draft"}</span> from{" "}
      <time dateTime={stamp} title={absoluteTime(stamp)}>
        {relativeTime(stamp)}
      </time>
    </>
  );
}
