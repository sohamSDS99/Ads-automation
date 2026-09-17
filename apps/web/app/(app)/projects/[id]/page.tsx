"use client";

import {
  ArrowLeft,
  CircleAlert,
  CircleCheck,
  History,
  Settings2,
  TriangleAlert,
} from "lucide-react";
import Link from "next/link";
import { use } from "react";

import { RunTrigger } from "@/components/projects/run-trigger";
import { Alert } from "@/components/ui/alert";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusPill } from "@/components/ui/status-pill";
import { buttonVariants } from "@/components/ui/button-variants";
import { blockers, warnings, type ProjectDetail } from "@/lib/api/projects";
import { absoluteTime, compactNumber, duration, relativeTime, usd } from "@/lib/format";
import { errorMessage, useProject } from "@/lib/queries";
import { Can } from "@/lib/session";
import { cn } from "@/lib/utils";

export default function ProjectOverviewPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const project = useProject(id);
  const error = errorMessage(project);

  if (error) {
    return (
      <div className="mx-auto max-w-4xl">
        <Alert tone="error" title="This project could not be loaded">
          {error}
        </Alert>
      </div>
    );
  }

  if (!project.data) {
    return (
      <div className="mx-auto flex max-w-4xl flex-col gap-6">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }

  return <Overview project={project.data} />;
}

function Overview({ project }: { project: ProjectDetail }) {
  const run = project.last_run;
  const stopped = blockers(project);
  const degraded = warnings(project);

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-6">
      <div>
        <Link
          href="/"
          className="inline-flex items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
        >
          <ArrowLeft className="size-4" aria-hidden />
          Projects
        </Link>
      </div>

      <header className="flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <h1 className="truncate text-[length:var(--text-xl)] font-semibold tracking-tight">
            {project.name}
          </h1>
          <p className="mt-1 font-mono text-sm text-fg-subtle">{project.domain}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Can permission="project_write">
            <Link
              href={`/projects/${project.id}/setup`}
              className={cn(buttonVariants({ variant: "secondary" }))}
            >
              <Settings2 aria-hidden />
              Set up
            </Link>
          </Can>
          <Can permission="run_execute">
            <RunTrigger project={project} />
          </Can>
        </div>
      </header>

      <Readiness blocking={stopped} degrading={degraded} />

      <div className="grid gap-4 sm:grid-cols-3">
        <Stat label="Last run">
          {run ? (
            <div className="space-y-1.5">
              {/* The pill is the way in: the console is where a run is read,
                  and hunting for it through the history table is a step
                  nobody needs. */}
              <Link
                href={`/projects/${project.id}/runs/${run.id}`}
                className="inline-flex rounded-full"
              >
                <StatusPill status={run.status} />
                <span className="sr-only">Open the run console</span>
              </Link>
              <p className="text-xs text-fg-subtle" title={absoluteTime(run.started_at)}>
                {relativeTime(run.started_at)}
                {run.triggered_by_name ? ` · ${run.triggered_by_name}` : ""}
                {run.trigger === "schedule" ? " · Schedule" : ""}
              </p>
              {run.status === "succeeded" ? (
                <Link
                  href={`/projects/${project.id}/runs/${run.id}/report`}
                  className="text-xs text-accent hover:underline"
                >
                  Read the report
                </Link>
              ) : null}
            </div>
          ) : (
            <p className="text-sm text-fg-muted">Never run</p>
          )}
        </Stat>
        <Stat label="Last run cost">
          <p data-numeric className="text-[length:var(--text-lg)] font-medium tracking-tight">
            {usd(run?.cost_usd ?? null)}
          </p>
          {/* Not a second em-dash under the first: two stacked dashes read as a
              rendering fault rather than as "there is nothing here yet". */}
          <p className="text-xs text-fg-subtle">
            {run ? `${compactNumber(run.token_in + run.token_out)} tokens` : "No runs yet"}
          </p>
        </Stat>
        <Stat label="Duration">
          <p data-numeric className="text-[length:var(--text-lg)] font-medium tracking-tight">
            {run ? duration(run.started_at, run.finished_at) : "—"}
          </p>
          <p className="text-xs text-fg-subtle">
            {project.run_count} {project.run_count === 1 ? "run" : "runs"} total
          </p>
        </Stat>
      </div>

      <Card>
        <CardHeader
          title="Research target"
          description="What every node in the run is grounded on."
        />
        <CardBody className="space-y-4">
          <dl className="grid gap-4 sm:grid-cols-2">
            <Detail term="Markets">
              {project.markets.length ? (
                <span>
                  {project.markets
                    .map((market) => `${market.country} · ${market.language} · ${market.currency}`)
                    .join(", ")}
                </span>
              ) : (
                <span className="text-fg-subtle">Not set</span>
              )}
            </Detail>
            <Detail term="Products">
              {project.product_context.products.length ? (
                project.product_context.products.join(", ")
              ) : (
                <span className="text-fg-subtle">Not set</span>
              )}
            </Detail>
          </dl>
          <div>
            <h3 className="text-xs font-medium tracking-wide text-fg-subtle">Business context</h3>
            <p className="mt-1.5 max-w-prose whitespace-pre-wrap text-sm leading-relaxed text-fg-muted">
              {project.product_context.summary || "Nothing written yet."}
            </p>
          </div>
        </CardBody>
      </Card>

      <Card>
        <CardHeader
          title="Approval gates"
          description="Three points where the run stops until a person decides."
        />
        <CardBody className="divide-y">
          {project.gates.map((gate) => (
            <div key={gate.node_id} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 py-3 first:pt-0 last:pb-0">
              <span className="font-mono text-xs text-fg-subtle">{gate.stage}</span>
              <span className="font-medium text-fg">{gate.name}</span>
              <span className="text-sm text-fg-muted">{gate.audience}</span>
              <span className="ml-auto text-sm">
                {gate.assignee_name ? (
                  <span className="text-fg">{gate.assignee_name}</span>
                ) : (
                  <span className="text-fg-subtle">Any approver</span>
                )}
                {gate.sla_hours ? (
                  <span className="text-fg-subtle"> · {gate.sla_hours}h</span>
                ) : null}
              </span>
            </div>
          ))}
        </CardBody>
      </Card>

      <Link
        href={`/projects/${project.id}/runs`}
        className="inline-flex items-center gap-2 text-sm text-fg-muted transition-colors hover:text-fg"
      >
        <History className="size-4" aria-hidden />
        Run history
      </Link>
    </div>
  );
}

/**
 * What the project still needs.
 *
 * Deliberately not called "readiness": that word belongs to the GO / NO-GO
 * verdict the research report reaches about the *account*, which arrives with
 * the report itself. This is about whether the run can start at all.
 */
function Readiness({
  blocking,
  degrading,
}: {
  blocking: { code: string; detail: string }[];
  degrading: { code: string; detail: string }[];
}) {
  if (blocking.length === 0 && degrading.length === 0) {
    return (
      <div className="flex items-center gap-2.5 rounded-[var(--radius)] border border-status-success/40 bg-surface px-3.5 py-3 text-sm">
        <CircleCheck className="size-4 shrink-0 text-status-success" aria-hidden />
        <span className="text-fg">Ready to run. Every source this project needs is connected.</span>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {blocking.length ? (
        <Alert tone="error" title={`${blocking.length} thing${blocking.length === 1 ? "" : "s"} to fix before a run`}>
          <ul className="space-y-1">
            {blocking.map((item) => (
              <li key={item.code} className="flex gap-2">
                <CircleAlert className="mt-0.5 size-3.5 shrink-0 text-status-failed" aria-hidden />
                {item.detail}
              </li>
            ))}
          </ul>
        </Alert>
      ) : null}
      {degrading.length ? (
        <Alert tone="warning" title="The run will start, with less evidence">
          <ul className="space-y-1">
            {degrading.map((item) => (
              <li key={item.code} className="flex gap-2">
                <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-status-gate" aria-hidden />
                {item.detail}
              </li>
            ))}
          </ul>
        </Alert>
      ) : null}
    </div>
  );
}

function Stat({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rounded-[var(--radius)] border bg-surface-raised px-4 py-3.5">
      <h2 className="text-xs font-medium tracking-wide text-fg-subtle">{label}</h2>
      <div className="mt-2">{children}</div>
    </div>
  );
}

function Detail({ term, children }: { term: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs font-medium tracking-wide text-fg-subtle">{term}</dt>
      <dd className="mt-1 text-sm text-fg">{children}</dd>
    </div>
  );
}
