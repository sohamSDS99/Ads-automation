"use client";

import {
  ArrowLeft,
  BookCheck,
  FlaskConical,
  Inbox,
  Info,
  PenLine,
  ScrollText,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { use } from "react";

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
  type GuidelineVersion,
} from "@/lib/api/guidelines";
import { absoluteTime, relativeTime } from "@/lib/format";
import { errorMessage, useGuidelineEligibility, useGuidelines, useProject } from "@/lib/queries";

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
          Four screens hang off this one and none of them is discoverable from
          a URL nobody types. They sit above the status card rather than below
          the history, because the two people who use this stage most — the
          legal owner with claims to sign, and the writer checking a headline —
          are not here to read the version history. */}
      <nav aria-label="Content guidelines sections" className="flex flex-wrap gap-2">
        <SectionLink href={`/projects/${id}/guidelines/claims`} icon={ScrollText}>
          Claims register
        </SectionLink>
        <SectionLink href={`/projects/${id}/guidelines/lint`} icon={FlaskConical}>
          Check your copy
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
                </span>
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
        <CardBody>
          {/* Person-tasks, expiring claims and unreviewed amendments land here
              from S3-P3, S3-P4 and S3-P9. Nothing writes any of them yet, so
              the block says so rather than rendering an empty list that looks
              like an answer. */}
          <p className="text-sm text-fg-muted">
            Signatures, expiring claims and policy amendments appear here once the claims register
            and the policy watcher ship.
          </p>
        </CardBody>
      </Card>

      {/* 4. History ------------------------------------------------------ */}
      <Card>
        <CardHeader title="Versions" />
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
            <Table label="Guideline versions">
              <thead>
                <Tr>
                  <Th>Version</Th>
                  <Th>Status</Th>
                  <Th>Scope</Th>
                  <Th>Created</Th>
                </Tr>
              </thead>
              <tbody>
                {versions.map((version) => (
                  <VersionRow key={version.id} projectId={id} version={version} />
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

function VersionRow({ projectId, version }: { projectId: string; version: GuidelineVersion }) {
  return (
    <Tr>
      <Td>
        <Link
          href={`/projects/${projectId}/runs/${version.guideline_run_id}`}
          className="font-medium underline-offset-2 hover:underline"
        >
          {versionLabel(version)}
        </Link>
      </Td>
      <Td>
        <Badge tone={version.status === "published" ? "accent" : "neutral"}>
          {version.status.replace(/_/g, " ")}
        </Badge>
      </Td>
      <Td className="text-fg-muted">
        {version.mode === "standalone" ? "unscoped" : version.mode.replace(/_/g, " ")}
      </Td>
      <Td className="text-fg-muted">
        <time dateTime={version.created_at} title={absoluteTime(version.created_at)}>
          {relativeTime(version.created_at)}
        </time>
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
