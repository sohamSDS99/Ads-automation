"use client";

import {
  ArrowLeft,
  CheckCircle2,
  FileWarning,
  Info,
  Package,
  PenLine,
  Scale,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { useState, type ReactNode } from "react";

import { MonoId } from "@/components/creative/mono-id";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import type { ApprovalItem } from "@/lib/api/approvals";
import {
  GATE_LABEL,
  WARNING_LABEL,
  blockerLabel,
  destinationLabel,
  isLiveCreativeRun,
  type CreativeEligibility,
  type CreativeOverview,
  type CreativePackageStatus,
  type CreativePackageSummary,
} from "@/lib/api/creative";
import type { HumanTask } from "@/lib/api/tasks";
import { absoluteTime, relativeTime, usd } from "@/lib/format";
import { errorMessage, useCreativeStatus, useProject } from "@/lib/queries";
import { useSession } from "@/lib/session";

/**
 * `/projects/[id]/creative` — the Stage 04 landing (PRD §15.4 A).
 *
 * Four blocks in the order a person arrives with the questions, as Stage 03's
 * landing has them: *what is the latest package*, *can I start*, *what is
 * waiting on somebody*, *what came before*.
 *
 * Unlike Stage 03 this stage is gated, and the landing is where the gate is
 * explained. The 04 entry on the rail stays a link when it is locked precisely
 * so that it lands here, on an Action block that names every blocker in the
 * server's own sentence and links to the stage that fixes it (§15.1 rule 1).
 * The two arrays come from `GET /creative/eligibility`; this file never
 * decides whether a run may start, and never promotes a warning to a blocker
 * or the other way round.
 *
 * One card on the page, and it is the Action block: §15.2 rule 4 keeps cards
 * for decisions, and the other three blocks are data.
 */
export function CreativeLanding({ projectId }: { projectId: string }) {
  const { user } = useSession();
  const project = useProject(projectId);
  const status = useCreativeStatus(projectId, user.id);
  const { eligibility, overview } = status;
  const [compare, setCompare] = useState<string[]>([]);

  const error = errorMessage(eligibility) ?? errorMessage(overview);
  if (error) {
    return (
      <div className="mx-auto w-full max-w-4xl">
        <Alert tone="error" title="Copy & creative could not be loaded">
          {error}
        </Alert>
      </div>
    );
  }

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-8">
      <header className="min-w-0">
        <Link
          href={`/projects/${projectId}`}
          className="inline-flex max-w-full items-center gap-2 text-sm text-fg-muted transition-colors hover:text-fg"
        >
          <ArrowLeft className="size-4 shrink-0" aria-hidden />
          <span className="truncate">{project.data?.name ?? "Project"}</span>
        </Link>
        <h1 className="mt-2 text-xl font-semibold tracking-tight">Copy &amp; creative</h1>
        <p className="mt-1 max-w-prose text-sm text-fg-muted">
          Writes the ads, extensions, images and video for a frozen campaign plan, each one checked
          against the published rulebook before anyone sees it. It needs both before it can start.
        </p>
      </header>

      <StatusBlock overview={overview.data} chip={status.chip} pending={overview.isPending} />

      <ActionBlock
        eligibility={eligibility.data}
        pending={eligibility.isPending}
        here={`/projects/${projectId}/creative`}
      />

      <AttentionBlock
        overview={overview.data}
        gates={status.gates}
        legal={status.legal}
        userId={user.id}
        pending={overview.isPending || !status.tasksLoaded}
      />

      <HistoryBlock
        overview={overview.data}
        pending={overview.isPending}
        compare={compare}
        onToggle={(packageId) =>
          // Two is the whole vocabulary of a diff; a third tick drops the
          // oldest rather than refusing the click (the Stage 03 rule).
          setCompare((current) =>
            current.includes(packageId)
              ? current.filter((item) => item !== packageId)
              : [...current, packageId].slice(-2),
          )
        }
      />
    </div>
  );
}

/** A block heading. A block is a `<section>` named by it. */
function Block({
  id,
  title,
  actions,
  children,
}: {
  id: string;
  title: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section aria-labelledby={id} className="flex flex-col gap-2">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id={id} className="text-base font-medium tracking-tight text-fg">
          {title}
        </h2>
        {actions}
      </div>
      {children}
    </section>
  );
}

const PACKAGE_STATUS: Record<CreativePackageStatus, { label: string; tone: "neutral" | "accent" | "warning" | "danger" }> = {
  draft: { label: "Draft", tone: "neutral" },
  blocked: { label: "Blocked", tone: "danger" },
  ready_to_release: { label: "Ready to release", tone: "accent" },
  released: { label: "Released", tone: "accent" },
  superseded: { label: "Superseded", tone: "neutral" },
};

function costOf(overview: CreativeOverview, pkg: CreativePackageSummary): string {
  const run = overview.runs.find((item) => item.run_id === pkg.creative_run_id);
  return run ? usd(run.cost_usd) : "—";
}

/* 1. Status ---------------------------------------------------------------- */

function StatusBlock({
  overview,
  chip,
  pending,
}: {
  overview?: CreativeOverview;
  chip: string | null;
  pending: boolean;
}) {
  const latestRun = overview?.runs[0];
  const newest = overview?.packages[0];

  return (
    <Block id="creative-status" title="Latest package">
      {pending || !overview ? (
        <Skeleton className="h-16 w-full" />
      ) : (
        <div className="flex flex-col gap-4 text-sm">
          {/* A live run is building the next package, so it is said first —
              with the same words the rail's chip uses, and the run's id in
              mono for whoever has to find it in the logs. */}
          {latestRun && isLiveCreativeRun(latestRun) ? (
            <p className="flex flex-wrap items-center gap-2 text-fg-muted">
              <Badge tone="accent">{chip ?? "Running"}</Badge>
              <span>
                Run <MonoId value={latestRun.run_id} label="run id" /> started{" "}
                <time
                  dateTime={latestRun.started_at ?? undefined}
                  title={absoluteTime(latestRun.started_at)}
                >
                  {latestRun.started_at ? relativeTime(latestRun.started_at) : "just now"}
                </time>
                {" · "}
                <span className="tabular-nums">{usd(latestRun.cost_usd)}</span> spent
              </span>
            </p>
          ) : null}

          {newest ? (
            <div className="flex flex-col gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-fg">Package v{newest.version}</span>
                <Badge tone={PACKAGE_STATUS[newest.status].tone}>
                  {PACKAGE_STATUS[newest.status].label}
                </Badge>
                {newest.plan_superseded ? <Badge tone="warning">Plan superseded</Badge> : null}
                {newest.ruleset_superseded ? (
                  <Badge tone="warning">Newer ruleset available</Badge>
                ) : null}
              </div>
              {/* The pins, because "what was this built against" is the
                  question a package answers before any other. */}
              <dl className="flex flex-wrap items-baseline gap-x-6 gap-y-1 text-fg-muted">
                <div className="flex items-baseline gap-2">
                  <dt>Plan</dt>
                  <dd className="tabular-nums text-fg">v{newest.plan_version}</dd>
                </div>
                <div className="flex items-baseline gap-2">
                  <dt>Ruleset</dt>
                  <dd className="font-mono text-xs text-fg">{newest.ruleset_version}</dd>
                </div>
                <div className="flex items-baseline gap-2">
                  <dt>Cost</dt>
                  <dd className="tabular-nums text-fg">{costOf(overview, newest)}</dd>
                </div>
                <div className="flex items-baseline gap-2">
                  <dt>Released</dt>
                  <dd className="text-fg">
                    {newest.released_at ? (
                      <time dateTime={newest.released_at} title={absoluteTime(newest.released_at)}>
                        {relativeTime(newest.released_at)}
                      </time>
                    ) : (
                      "Not yet"
                    )}
                  </dd>
                </div>
              </dl>
            </div>
          ) : latestRun && isLiveCreativeRun(latestRun) ? null : (
            <p className="max-w-prose text-fg-muted">
              No package yet. A package is every approved headline, extension and rendition from
              one run, released as a version that launch reads and nothing can change.
            </p>
          )}
        </div>
      )}
    </Block>
  );
}

/* 2. Action ---------------------------------------------------------------- */

/**
 * Can a run start, and if not, why not — every blocker, then every warning.
 *
 * The lead phrase names the code; the sentence after it is the server's,
 * verbatim, because it was written about this project and a paraphrase is how
 * "unavailable" comes back. The link goes wherever the server says the fix
 * is, and its text is read off that URL.
 *
 * When nothing blocks, the block says so and states what a run started now
 * would pin. The dialog that chooses scope and models and starts the run is
 * S4-P3's and mounts here.
 */
function ActionBlock({
  eligibility,
  pending,
  here,
}: {
  eligibility?: CreativeEligibility;
  pending: boolean;
  /** This page's own path: a note whose fix is "here" gets no link to itself. */
  here: string;
}) {
  return (
    <Card>
      <CardHeader title="Start a run" />
      <CardBody className="flex flex-col gap-4">
        {pending || !eligibility ? (
          <Skeleton className="h-10 w-full" />
        ) : (
          <>
            {eligibility.blockers.length > 0 ? (
              <div className="flex flex-col gap-2">
                <p className="text-sm text-fg">
                  {eligibility.blockers.length === 1
                    ? "One thing has to change before a run can start."
                    : `${eligibility.blockers.length} things have to change before a run can start.`}
                </p>
                <ul className="flex flex-col divide-y rounded-token border">
                  {eligibility.blockers.map((note) => (
                    <NoteRow
                      key={note.code}
                      icon={TriangleAlert}
                      tone="blocker"
                      label={blockerLabel(note)}
                      detail={note.detail}
                      href={note.fix_url === here ? null : note.fix_url}
                    />
                  ))}
                </ul>
              </div>
            ) : (
              <div className="flex flex-col gap-2 text-sm">
                <p className="flex items-center gap-2 font-medium text-fg">
                  <CheckCircle2 className="size-4 text-status-success" aria-hidden />
                  Nothing is blocking a run
                </p>
                <Pins pins={eligibility.pins} />
              </div>
            )}

            {eligibility.warnings.length > 0 ? (
              <ul className="flex flex-col divide-y rounded-token border">
                {eligibility.warnings.map((note) => (
                  <NoteRow
                    key={note.code}
                    icon={Info}
                    tone="warning"
                    label={WARNING_LABEL[note.code] ?? note.code}
                    detail={note.detail}
                    href={note.fix_url === here ? null : note.fix_url}
                  />
                ))}
              </ul>
            ) : null}
          </>
        )}
      </CardBody>
    </Card>
  );
}

/** What a run started now would pin (§4.4), from the eligibility answer. */
function Pins({ pins }: { pins: CreativeEligibility["pins"] }) {
  const plan = pins.plan_version;
  const ruleset = pins.ruleset_version;
  const context = pins.context_hash;
  return (
    <dl className="flex flex-wrap items-baseline gap-x-6 gap-y-1 text-fg-muted">
      <div className="flex items-baseline gap-2">
        <dt>Plan</dt>
        <dd className="tabular-nums text-fg">{plan !== undefined && plan !== null ? `v${plan}` : "—"}</dd>
      </div>
      <div className="flex items-baseline gap-2">
        <dt>Ruleset</dt>
        <dd className="font-mono text-xs text-fg">{ruleset ?? "—"}</dd>
      </div>
      {typeof context === "string" ? (
        <div className="flex items-baseline gap-2">
          <dt>Context</dt>
          <dd>
            <MonoId value={context} label="context hash" />
          </dd>
        </div>
      ) : null}
    </dl>
  );
}

function NoteRow({
  icon: Icon,
  tone,
  label,
  detail,
  href,
}: {
  icon: LucideIcon;
  tone: "blocker" | "warning";
  label: string;
  detail: string;
  href: string | null;
}) {
  return (
    <li className="flex items-start gap-2 px-4 py-2 text-sm">
      <Icon
        className={
          tone === "blocker"
            ? "mt-0.5 size-4 shrink-0 text-status-failed"
            : "mt-0.5 size-4 shrink-0 text-status-gate"
        }
        aria-hidden
      />
      <div className="min-w-0 flex-1">
        <p className="text-fg">
          <span className="font-medium">{label}</span>
          <span className="text-fg-muted"> · {detail}</span>
        </p>
        {href ? (
          <Link
            href={href}
            className="mt-1 inline-block font-medium text-accent underline-offset-2 hover:underline"
          >
            {destinationLabel(href)}
          </Link>
        ) : null}
      </div>
    </li>
  );
}

/* 3. Attention ------------------------------------------------------------- */

/**
 * What is waiting on a person: the open gate on the latest run, open legal
 * exceptions, and a newest package built on a plan or ruleset that has since
 * moved on (§15.4 A).
 *
 * Every row names who owes it — "you" when it is the reader, because the
 * difference between "somebody has to sign" and "you have to sign" is the
 * difference between a notice and a task. Decisions happen in the existing
 * approvals inbox (§15.3: no new inbox), so that is where each row links.
 */
function AttentionBlock({
  overview,
  gates,
  legal,
  userId,
  pending,
}: {
  overview?: CreativeOverview;
  gates: ApprovalItem[];
  legal: HumanTask[];
  userId: string;
  pending: boolean;
}) {
  const newest = overview?.packages[0];
  const quiet =
    gates.length === 0 && legal.length === 0 && !newest?.plan_superseded && !newest?.ruleset_superseded;

  return (
    <Block id="creative-attention" title="Needs attention">
      {pending ? (
        <Skeleton className="h-16 w-full" />
      ) : quiet ? (
        <EmptyState
          icon={CheckCircle2}
          title="Nothing is waiting on a person"
          description="No open sign-off, no legal exception, and the newest package was built on the current plan and ruleset."
          className="rounded-token border"
        />
      ) : (
        <ul className="flex flex-col divide-y rounded-token border">
          {gates.map((gate) => (
            <AttentionRow
              key={gate.id}
              icon={PenLine}
              tone={gate.can_decide ? "danger" : "neutral"}
              title={`${GATE_LABEL[gate.gate_key] ?? gate.gate_key} is waiting`}
              chip={gate.gate_key}
              href="/approvals"
              action="Open approvals"
            >
              {gate.can_decide
                ? "You can decide it"
                : `Assigned to ${gate.assignee_email ?? `an ${gate.required_role}`}`}
              {" · opened "}
              <time dateTime={gate.created_at} title={absoluteTime(gate.created_at)}>
                {relativeTime(gate.created_at)}
              </time>
            </AttentionRow>
          ))}

          {legal.map((task) => (
            <AttentionRow
              key={task.id}
              icon={Scale}
              tone={task.assignee_id === userId ? "danger" : "neutral"}
              title={task.title}
              chip="H3"
              href="/approvals"
              action="Open signatures & attestations"
            >
              {task.assignee_id === userId
                ? "Assigned to you"
                : `Assigned to ${task.assignee_name || task.assignee_email || "the legal owner"}`}
              {task.due_at ? (
                <>
                  {" · due "}
                  <time dateTime={task.due_at} title={absoluteTime(task.due_at)}>
                    {relativeTime(task.due_at)}
                  </time>
                </>
              ) : null}
            </AttentionRow>
          ))}

          {newest?.plan_superseded ? (
            <AttentionRow
              icon={FileWarning}
              tone="warning"
              title={`The plan behind package v${newest.version} was superseded`}
              chip="plan_superseded"
            >
              Package v{newest.version} still carries plan v{newest.plan_version}. A new run pins
              the plan that is frozen now.
            </AttentionRow>
          ) : null}

          {newest?.ruleset_superseded ? (
            <AttentionRow
              icon={FileWarning}
              tone="warning"
              title="A newer ruleset is available"
              chip="newer_ruleset_available"
            >
              Package v{newest.version} was checked against ruleset{" "}
              <span className="font-mono text-xs">{newest.ruleset_version}</span>. It is not
              re-checked on its own; a new run pins the newest published ruleset.
            </AttentionRow>
          ) : null}
        </ul>
      )}
    </Block>
  );
}

function AttentionRow({
  icon: Icon,
  tone,
  title,
  chip,
  href,
  action,
  children,
}: {
  icon: LucideIcon;
  tone: "neutral" | "warning" | "danger";
  title: string;
  chip: string;
  href?: string;
  action?: string;
  children: ReactNode;
}) {
  return (
    <li className="flex items-start gap-2 px-4 py-2">
      <Icon
        className={
          tone === "danger"
            ? "mt-0.5 size-4 shrink-0 text-status-failed"
            : tone === "warning"
              ? "mt-0.5 size-4 shrink-0 text-status-gate"
              : "mt-0.5 size-4 shrink-0 text-fg-subtle"
        }
        aria-hidden
      />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-sm font-medium text-fg">{title}</p>
          <Badge tone={tone === "danger" ? "danger" : tone === "warning" ? "warning" : "neutral"}>
            <span className="font-mono">{chip}</span>
          </Badge>
        </div>
        <p className="mt-0.5 text-sm text-fg-muted">{children}</p>
        {href && action ? (
          <Link
            href={href}
            className="mt-1 inline-block text-sm font-medium text-accent underline-offset-2 hover:underline"
          >
            {action}
          </Link>
        ) : null}
      </div>
    </li>
  );
}

/* 4. History --------------------------------------------------------------- */

function HistoryBlock({
  overview,
  pending,
  compare,
  onToggle,
}: {
  overview?: CreativeOverview;
  pending: boolean;
  compare: string[];
  onToggle: (packageId: string) => void;
}) {
  const packages = overview?.packages ?? [];
  return (
    <Block
      id="creative-history"
      title="Packages"
      actions={
        // The ticks are this page's; the diff they open is S4-P23's, so the
        // page says what it cannot do yet instead of linking to a route that
        // does not exist (Next would prefetch it and 404).
        compare.length === 2 ? (
          <span className="text-sm text-fg-subtle">2 selected. Package comparison is not available yet.</span>
        ) : compare.length === 1 ? (
          <span className="text-sm text-fg-subtle">Pick one more to compare</span>
        ) : null
      }
    >
      {pending || !overview ? (
        <Skeleton className="h-20 w-full" />
      ) : packages.length === 0 ? (
        <EmptyState
          icon={Package}
          title="No packages yet"
          description="A package appears here when a run assembles one. Releasing it makes it the version launch reads."
          className="rounded-token border"
        />
      ) : (
        // Three columns drop below `sm`: at 390px the version, its status,
        // its cost and the compare tick are the whole job (the Stage 03
        // landing measured a 403px page scroll with them all showing).
        <div className="rounded-token border">
          <Table label="Creative packages, newest first">
            <thead>
              <Tr>
                <Th>Version</Th>
                <Th>Status</Th>
                <Th className="hidden sm:table-cell">Plan</Th>
                <Th className="hidden sm:table-cell">Ruleset</Th>
                <Th className="hidden text-right sm:table-cell">Cost</Th>
                <Th className="hidden sm:table-cell">Released</Th>
                <Th>Compare</Th>
              </Tr>
            </thead>
            <tbody>
              {packages.map((pkg) => (
                <Tr key={pkg.package_id}>
                  <Td className="font-medium tabular-nums">
                    v{pkg.version}
                    {/* Below `sm` the cost rides under the version, as Stage
                        03's history carries its date: four columns clipped
                        the compare tick at 390px. */}
                    <span className="block text-xs font-normal text-fg-subtle sm:hidden">
                      {costOf(overview, pkg)}
                    </span>
                  </Td>
                  <Td>
                    <Badge tone={PACKAGE_STATUS[pkg.status].tone}>
                      {PACKAGE_STATUS[pkg.status].label}
                    </Badge>
                  </Td>
                  <Td className="hidden tabular-nums text-fg-muted sm:table-cell">
                    v{pkg.plan_version}
                    {pkg.plan_superseded ? <span className="sr-only"> (superseded)</span> : null}
                  </Td>
                  <Td className="hidden font-mono text-xs text-fg-muted sm:table-cell">
                    {pkg.ruleset_version}
                  </Td>
                  <Td className="hidden text-right tabular-nums sm:table-cell">{costOf(overview, pkg)}</Td>
                  <Td className="hidden text-fg-muted sm:table-cell">
                    {pkg.released_at ? (
                      <time dateTime={pkg.released_at} title={absoluteTime(pkg.released_at)}>
                        {relativeTime(pkg.released_at)}
                      </time>
                    ) : (
                      "—"
                    )}
                  </Td>
                  <Td>
                    <label className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={compare.includes(pkg.package_id)}
                        onChange={() => onToggle(pkg.package_id)}
                        className="size-4 accent-accent"
                      />
                      <span className="sr-only">Compare package v{pkg.version}</span>
                    </label>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </div>
      )}
    </Block>
  );
}
