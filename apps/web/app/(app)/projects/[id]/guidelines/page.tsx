"use client";

import {
  ArrowLeft,
  BookCheck,
  CalendarClock,
  CheckCircle2,
  FileWarning,
  FlaskConical,
  Inbox,
  Info,
  PenLine,
  ScrollText,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { use, useState } from "react";

import { StartGuidelineDialog } from "@/components/guidelines/start-guideline-dialog";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import {
  versionLabel,
  type EligibilityNote,
  type GuidelineAttention,
  type GuidelineVersion,
  type OpenTaskRef,
} from "@/lib/api/guidelines";
import { absoluteTime, relativeTime } from "@/lib/format";
import {
  errorMessage,
  useGuidelineAttention,
  useGuidelineEligibility,
  useGuidelines,
  useProject,
  usePublishedRuleSet,
} from "@/lib/queries";

/**
 * `/projects/[id]/guidelines` — the Stage 03 landing (PRD §15.3 A).
 *
 * Four stacked blocks in the order a person arrives with the questions:
 * *what is published right now*, *can I start*, *what needs my attention*,
 * *what came before*.
 *
 * The block that matters most is the second, and what matters about it is what
 * it does **not** do. Stage 02's landing leads with a lock when planning
 * cannot start. This page has no lock: warnings render as notes beside an
 * enabled button, and only the `blockers` array can disable anything. The two
 * arrays come from the server precisely so this file cannot get that wrong by
 * forgetting a `severity` filter.
 *
 * Every role can read this page. A `viewer` sees the same four blocks without
 * the Start button.
 */
export default function GuidelinesLandingPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const project = useProject(id);
  const eligibility = useGuidelineEligibility(id);
  const guidelines = useGuidelines(id);
  const attention = useGuidelineAttention(id);
  // A 404 here is the answer "nothing is published" (contract rule 4), so it
  // is read as data and never as a failure — hence `isSuccess` rather than a
  // truthiness check on `data`.
  const ruleset = usePublishedRuleSet(id);
  const [compare, setCompare] = useState<string[]>([]);

  const error = errorMessage(eligibility);
  if (error) {
    return (
      <div className="mx-auto w-full max-w-4xl">
        <Alert tone="error" title="Content guidelines could not be loaded">
          {error}
        </Alert>
      </div>
    );
  }

  const versions = guidelines.data?.versions ?? [];
  const published = versions.find((item) => item.status === "published");

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6">
      <header className="min-w-0">
        <Link
          href={`/projects/${id}`}
          className="inline-flex max-w-full items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
        >
          <ArrowLeft className="size-4 shrink-0" aria-hidden />
          <span className="truncate">{project.data?.name ?? "Project"}</span>
        </Link>
        <h1 className="mt-2 text-[length:var(--text-xl)] font-semibold tracking-tight">
          Content guidelines
        </h1>
        <p className="mt-1 max-w-prose text-sm text-fg-muted">
          Turns what we are allowed to say into a rulebook every headline and image is checked
          against. Startable on its own — research and a plan narrow it, and neither is required.
        </p>
      </header>

      {/* 0. Where else this stage lives ---------------------------------
          Six screens hang off this one and none of them is discoverable from a
          URL nobody types. They sit above the status card rather than below the
          history, because the two people who use this stage most — the legal
          owner with claims to sign, and the writer checking a headline — are
          not here to read the version history.

          This is a directory and therefore lists every screen, including the
          two the published-version card below also links to. That card's links
          are contextual — they sit under that version's own rule count and
          read as "this one" — and a directory that omitted a screen because
          something else happened to mention it would be a directory you cannot
          trust. */}
      <nav aria-label="Content guidelines sections" className="flex flex-wrap gap-2">
        <SectionLink href={`/projects/${id}/guidelines/published`} icon={BookCheck}>
          Published rulebook
        </SectionLink>
        <SectionLink href={`/projects/${id}/guidelines/claims`} icon={ScrollText}>
          Claims register
        </SectionLink>
        <SectionLink href={`/projects/${id}/guidelines/lint`} icon={FlaskConical}>
          Check your copy
        </SectionLink>
        <SectionLink href={`/projects/${id}/guidelines/specs`} icon={FileWarning}>
          Asset specs
        </SectionLink>
        <SectionLink href={`/projects/${id}/guidelines/amendments`} icon={Inbox}>
          Amendments &amp; sign-off
        </SectionLink>
        <SectionLink href="/approvals" icon={PenLine}>
          Signatures &amp; attestations
        </SectionLink>
      </nav>

      {/* 1. Status ------------------------------------------------------- */}
      <Card>
        <CardHeader title="Published rulebook" />
        <CardBody>
          {guidelines.isPending ? (
            <Skeleton className="h-16 w-full" />
          ) : published ? (
            <div className="flex flex-col gap-2 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone="accent">{versionLabel(published)}</Badge>
                {published.signature_stale ? (
                  <Badge tone="warning">Signature stale</Badge>
                ) : null}
                {published.binding_superseded ? (
                  <Badge tone="warning">Binding superseded</Badge>
                ) : null}
                <span className="text-fg-muted">
                  published{" "}
                  <time dateTime={published.published_at ?? undefined}>
                    {published.published_at ? relativeTime(published.published_at) : "—"}
                  </time>
                  {published.published_by_name ? ` by ${published.published_by_name}` : ""}
                </span>
              </div>
              {/* The pin Stage 04 enforces against. Monospace because it is an
                  identifier somebody will copy into a pin, and stated here
                  rather than only inside the rulebook because "which rules are
                  live right now" is the question this block exists to answer. */}
              <dl className="flex flex-wrap items-baseline gap-x-6 gap-y-1 text-fg-muted">
                <div className="flex items-baseline gap-2">
                  <dt>Ruleset</dt>
                  <dd data-numeric className="font-mono text-xs text-fg">
                    {ruleset.isSuccess ? ruleset.data.ruleset_version : "—"}
                  </dd>
                </div>
                <div className="flex items-baseline gap-2">
                  <dt>Scope</dt>
                  <dd className="text-fg">{modeLabel(published.mode)}</dd>
                </div>
                <div className="flex items-baseline gap-2">
                  <dt>Rules</dt>
                  <dd data-numeric className="text-fg">
                    {ruleset.isSuccess ? ruleset.data.rule_count : published.rule_count}
                  </dd>
                </div>
              </dl>
              <div className="flex flex-wrap gap-3 pt-1">
                <Link
                  href={`/projects/${id}/guidelines/published`}
                  className="font-medium text-accent underline-offset-2 hover:underline"
                >
                  Read the rulebook
                </Link>
                <Link
                  href={`/projects/${id}/guidelines/specs`}
                  className="text-fg-muted underline-offset-2 hover:text-fg hover:underline"
                >
                  Asset specs
                </Link>
              </div>
              {/* Stated plainly rather than hidden behind a tooltip: a rulebook
                  built without legal guardrails must not read as authoritative
                  as one built with them (§4.3 rule 2). */}
              {published.unbound_inputs.length > 0 ? (
                <p className="text-fg-muted">
                  Built without: {published.unbound_inputs.join(", ")}.
                </p>
              ) : null}
            </div>
          ) : (
            <p className="text-sm text-fg-muted">
              Nothing published yet. Until a version is published, Stage 04 has no ruleset to lint
              against and cannot produce creative.
            </p>
          )}
        </CardBody>
      </Card>

      {/* 2. Action ------------------------------------------------------- */}
      <Card>
        <CardHeader title="Start a run" />
        <CardBody className="flex flex-col gap-4">
          {eligibility.isPending ? (
            <Skeleton className="h-10 w-56" />
          ) : (
            <>
              {/* Blockers only. A warning below never reaches this list. */}
              {eligibility.data?.blockers.map((note) => (
                <Note key={note.code} note={note} tone="blocker" />
              ))}
              {eligibility.data?.warnings.map((note) => (
                <Note key={note.code} note={note} tone="warning" />
              ))}
              {eligibility.data ? (
                <div>
                  <StartGuidelineDialog
                    projectId={id}
                    available={eligibility.data.available_bindings}
                    disabled={!eligibility.data.eligible}
                  />
                </div>
              ) : null}
            </>
          )}
        </CardBody>
      </Card>

      {/* 3. Attention ---------------------------------------------------- */}
      <Card>
        <CardHeader title="Needs attention" />
        <CardBody className="p-0">
          {attention.isPending ? (
            <div className="p-4">
              <Skeleton className="h-16 w-full" />
            </div>
          ) : (
            <Attention data={attention.data} />
          )}
        </CardBody>
      </Card>

      {/* 4. History ------------------------------------------------------ */}
      <Card>
        <CardHeader
          title="Versions"
          actions={
            compare.length === 2 ? (
              // The checkbox is this phase's; the diff it opens is S3-P8's.
              <span className="text-sm text-fg-subtle">
                Two selected — the version diff arrives with the compare view
              </span>
            ) : compare.length === 1 ? (
              <span className="text-sm text-fg-subtle">Pick one more to compare</span>
            ) : null
          }
        />
        <CardBody className="p-0">
          {guidelines.isPending ? (
            <div className="p-4">
              <Skeleton className="h-20 w-full" />
            </div>
          ) : versions.length === 0 ? (
            <EmptyState
              icon={BookCheck}
              title="No guideline runs yet"
              description="Start one above. It does not need research or a plan first."
            />
          ) : (
            // `Table` brings its own `overflow-x-auto` and it is not enough
            // here: `min-w-max` sizes the table to its content and the clip
            // never reaches the document, so the *page* scrolls sideways —
            // measured at 403px at a 390px viewport before these five columns
            // were dropped below `sm`. The fix is structural rather than a
            // scroller inside a card: on a phone the version, its status and
            // the compare tick are the whole job, and the detail returns at
            // `sm`. The date rides under the version label meanwhile.
            <Table label="Guideline versions">
              <thead>
                <Tr>
                  <Th>Version</Th>
                  <Th>Status</Th>
                  <Th className="hidden sm:table-cell">Scope</Th>
                  <Th className="hidden sm:table-cell">Rules</Th>
                  <Th className="hidden sm:table-cell">Claims</Th>
                  <Th className="hidden sm:table-cell">Published by</Th>
                  <Th className="hidden sm:table-cell">Created</Th>
                  <Th>Compare</Th>
                </Tr>
              </thead>
              <tbody>
                {versions.map((version) => (
                  <VersionRow
                    key={version.id}
                    projectId={id}
                    version={version}
                    checked={compare.includes(version.id)}
                    // Two is the whole vocabulary of a diff. A third tick
                    // drops the oldest rather than refusing the click: a
                    // disabled checkbox on a row somebody just aimed at reads
                    // as broken, and there is no third thing it could mean.
                    onToggle={() =>
                      setCompare((current) =>
                        current.includes(version.id)
                          ? current.filter((item) => item !== version.id)
                          : [...current, version.id].slice(-2),
                      )
                    }
                  />
                ))}
              </tbody>
            </Table>
          )}
        </CardBody>
      </Card>
    </div>
  );
}

/**
 * One eligibility note.
 *
 * The tone is passed in by the caller from *which array the note came out of*,
 * never derived from the note itself. There is no severity field to read and
 * no code list to keep in sync here — which is the point of the server
 * returning two arrays.
 */
function Note({ note, tone }: { note: EligibilityNote; tone: "blocker" | "warning" }) {
  const Icon = tone === "blocker" ? TriangleAlert : Info;
  return (
    <p
      className={
        tone === "blocker"
          ? "flex items-start gap-2 rounded-[var(--radius)] bg-danger-soft p-3 text-sm text-danger"
          : "flex items-start gap-2 rounded-[var(--radius)] bg-surface-sunken p-3 text-sm text-fg-muted"
      }
    >
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden />
      <span>
        {/* Rendered verbatim. The server wrote a whole sentence about this
            project; paraphrasing it here is how "unavailable" comes back. */}
        {note.detail}{" "}
        <Link href={note.fix_url} className="underline underline-offset-2">
          Open
        </Link>
      </span>
    </p>
  );
}

/**
 * What a run was allowed to read, said the way a person would say it.
 *
 * `standalone` is the cold-start mode and the word for it is *unscoped*, not
 * "standalone": what the reader needs to know is that the rules apply to
 * everything because nothing narrowed them, which is law 21 stated in one
 * word (`RuleScope`: an empty scope means everywhere).
 */
function modeLabel(mode: GuidelineVersion["mode"]): string {
  return mode === "standalone" ? "unscoped" : mode.replace(/_/g, " ");
}

/**
 * Block 3 — what on this project is waiting for a human (§15.3 A).
 *
 * Person-tasks are listed for *anyone*, not only the reader: a rulebook that
 * cannot be published because somebody else has not signed is the reader's
 * problem too, and the thing they need is the name to go and ask.
 *
 * Each row links to the screen that actions it. Those screens are S3-P8's, so
 * the link says what will be there rather than pretending to be it.
 */
function Attention({ data }: { data?: GuidelineAttention }) {
  if (!data) {
    return (
      <p className="p-4 text-sm text-fg-muted">
        Could not read what is outstanding on this project.
      </p>
    );
  }

  const quiet =
    data.open_tasks_total === 0 &&
    data.expiring_claims === 0 &&
    data.unreviewed_amendments === 0 &&
    !data.signature_stale;

  if (quiet) {
    return (
      <EmptyState
        icon={CheckCircle2}
        title="Nothing is waiting on a person"
        description="No open signatures or attestations, no claims expiring in the next 30 days, and no policy changes to rule on."
      />
    );
  }

  return (
    <ul className="divide-y">
      {data.open_tasks.map((task) => (
        <TaskRow key={task.task_id} task={task} />
      ))}

      {data.open_tasks_total > data.open_tasks.length ? (
        <li className="px-4 py-2 text-sm text-fg-subtle">
          and {data.open_tasks_total - data.open_tasks.length} more open{" "}
          {data.open_tasks_total - data.open_tasks.length === 1 ? "task" : "tasks"}
        </li>
      ) : null}

      {data.expiring_claims > 0 ? (
        <AttentionRow
          icon={CalendarClock}
          tone="warning"
          soon="Renew or retire them in the claims register."
          title={
            data.expiring_claims === 1
              ? "1 claim expires soon"
              : `${data.expiring_claims} claims expire soon`
          }
        >
          {/* The date, not just the count: "within 30 days" is a window and
              the thing that decides whether this is today's problem is where
              in the window the first one falls. */}
          Their licence lapses within {data.expiry_window_days} days
          {data.earliest_expiry ? (
            <>
              , the first on{" "}
              <time dateTime={data.earliest_expiry}>{absoluteTime(data.earliest_expiry)}</time>
            </>
          ) : null}
          . Once one lapses the linter stops licensing its wording.
        </AttentionRow>
      ) : null}

      {data.unreviewed_amendments > 0 ? (
        <AttentionRow
          icon={FileWarning}
          tone={data.signature_affecting_amendments > 0 ? "danger" : "warning"}
          soon="Rule on each change in the amendment inbox."
          title={
            data.unreviewed_amendments === 1
              ? "1 policy amendment is unreviewed"
              : `${data.unreviewed_amendments} policy amendments are unreviewed`
          }
        >
          {data.signature_affecting_amendments > 0 ? (
            <>
              {data.signature_affecting_amendments} of them{" "}
              {data.signature_affecting_amendments === 1 ? "voids a" : "void"} signature and
              re-queues the claims it covered.
            </>
          ) : (
            <>Google changed a policy this rulebook cites. Somebody has to rule on each change.</>
          )}
        </AttentionRow>
      ) : null}

      {data.signature_stale ? (
        <AttentionRow
          icon={PenLine}
          tone="danger"
          soon="Re-sign the register to license those claims again."
          title="The published rulebook's signature is stale"
        >
          It is still serving, and the claims its signature covered are no longer licensed.
        </AttentionRow>
      ) : null}
    </ul>
  );
}

function TaskRow({ task }: { task: OpenTaskRef }) {
  return (
    <AttentionRow
      icon={PenLine}
      tone={task.mine ? "danger" : "neutral"}
      soon={task.task_key === "H1" ? "Signed in the claims register." : "Submitted on the person-task card."}
      title={task.title}
      chip={task.blocking_for === "launch" ? "Blocks launch" : "Blocks publish"}
    >
      {/* Who owes it, said in the second person when it is the reader — the
          difference between "somebody has to sign" and "you have to sign" is
          the difference between a notice and a task. */}
      {task.mine ? "Assigned to you" : `Assigned to ${task.assignee_name || "someone"}`}
      {task.due_at ? (
        <>
          {" · due "}
          <time dateTime={task.due_at}>{absoluteTime(task.due_at)}</time>
        </>
      ) : null}
    </AttentionRow>
  );
}

/**
 * One outstanding thing.
 *
 * `soon` rather than `href`, and that is deliberate. The screens that action
 * these — the claims register, the signature drawer, the amendment inbox —
 * are S3-P8's. A link to a route that does not exist yet 404s when somebody
 * clicks it, and Next prefetches it before they do, so the dead end reaches
 * the console of everyone who merely *looks* at this page. Naming where the
 * work will happen is honest; linking to nothing is not. S3-P8 turns each
 * `soon` back into an `href`.
 */
function AttentionRow({
  icon: Icon,
  tone,
  soon,
  title,
  chip,
  children,
}: {
  icon: typeof PenLine;
  tone: "neutral" | "warning" | "danger";
  /** Where this will be actioned, in the words a person would use. */
  soon: string;
  title: string;
  chip?: string;
  children: React.ReactNode;
}) {
  return (
    <li className="flex items-start gap-3 px-4 py-3">
      <Icon
        className={
          tone === "danger"
            ? "mt-0.5 size-4 shrink-0 text-[var(--status-failed)]"
            : tone === "warning"
              ? "mt-0.5 size-4 shrink-0 text-[var(--status-gate)]"
              : "mt-0.5 size-4 shrink-0 text-fg-subtle"
        }
        aria-hidden
      />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-sm font-medium text-fg">{title}</p>
          {chip ? <Badge tone={tone === "danger" ? "danger" : "neutral"}>{chip}</Badge> : null}
        </div>
        <p className="mt-0.5 text-sm text-fg-muted">{children}</p>
        <p className="mt-0.5 text-xs text-fg-subtle">{soon}</p>
      </div>
    </li>
  );
}

function VersionRow({
  projectId,
  version,
  checked,
  onToggle,
}: {
  projectId: string;
  version: GuidelineVersion;
  checked: boolean;
  onToggle: () => void;
}) {
  const label = versionLabel(version);
  return (
    <Tr>
      <Td>
        <Link
          href={`/projects/${projectId}/guidelines/runs/${version.guideline_run_id}`}
          className="font-medium underline-offset-2 hover:underline"
        >
          {label}
        </Link>
        <time dateTime={version.created_at} className="block text-xs text-fg-subtle sm:hidden">
          {relativeTime(version.created_at)}
        </time>
      </Td>
      <Td>
        <Badge tone={version.status === "published" ? "accent" : "neutral"}>
          {version.status.replace(/_/g, " ")}
        </Badge>
      </Td>
      <Td className="hidden text-fg-muted sm:table-cell">{modeLabel(version.mode)}</Td>
      <Td data-numeric className="hidden text-fg-muted sm:table-cell">
        {version.rule_count}
      </Td>
      <Td data-numeric className="hidden text-fg-muted sm:table-cell">
        {version.claim_count}
      </Td>
      <Td className="hidden text-fg-muted sm:table-cell">
        {version.published_by_name || "—"}
      </Td>
      <Td className="hidden text-fg-muted sm:table-cell">
        <time dateTime={version.created_at} title={absoluteTime(version.created_at)}>
          {relativeTime(version.created_at)}
        </time>
      </Td>
      <Td>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={checked}
            onChange={onToggle}
            className="size-4 accent-[var(--accent)]"
          />
          {/* The row's version is already in the first column, but a bare
              checkbox has no accessible name of its own and "checkbox" is not
              one. */}
          <span className="sr-only">Compare {label === "—" ? "this draft" : label}</span>
        </label>
      </Td>
    </Tr>
  );
}

/**
 * One section link.
 *
 * A link and not a card: four same-size cards of icon-plus-heading is the lazy
 * page scaffold, and these are navigation, not content.
 */
function SectionLink({
  href,
  icon: Icon,
  children,
}: {
  href: string;
  icon: LucideIcon;
  children: React.ReactNode;
}) {
  return (
    <Link
      href={href}
      className="inline-flex items-center gap-2 rounded-[var(--radius)] border bg-surface-raised px-3 py-2 text-sm text-fg transition-colors hover:bg-surface-hover"
    >
      <Icon aria-hidden className="size-4 text-fg-subtle" />
      {children}
    </Link>
  );
}
