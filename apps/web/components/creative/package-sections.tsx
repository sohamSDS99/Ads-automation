"use client";

import {
  CircleCheck,
  CircleDashed,
  CircleMinus,
  CircleX,
  Download,
  FileText,
  Folder,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";
import { type ReactNode } from "react";

import { MonoId } from "@/components/creative/mono-id";
import { Badge } from "@/components/ui/badge";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import {
  packageFileUrl,
  type ChecklistItem,
  type CostSummary,
  type CreativeCritique,
  type LintSummary,
  type ManifestEntry,
  type PackageDependency,
  type PackageLintVerdict,
  type PackagePins,
  type ReleaseGate,
  type ReleaseStop,
} from "@/lib/api/creative-packages";
import { costDelta, formatBytes, manifestTree, type ManifestNode } from "@/lib/creative/package";
import { absoluteTime, relativeTime, usd } from "@/lib/format";
import { cn } from "@/lib/utils";

/** A titled block of the package screen or the released page. */
export function PackageSection({
  id,
  title,
  meta,
  children,
}: {
  id: string;
  title: string;
  meta?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section aria-labelledby={id} className="flex min-w-0 flex-col gap-3" data-testid={id}>
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-b pb-2">
        <h2 id={id} className="text-md font-medium tracking-tight text-fg">
          {title}
        </h2>
        {meta}
      </div>
      {children}
    </section>
  );
}

function When({ at }: { at: string | null }) {
  if (!at) return <>—</>;
  return (
    <time dateTime={at} title={absoluteTime(at)} className="tabular-nums">
      {relativeTime(at)}
    </time>
  );
}

// ---------------------------------------------------------------------------
// the three stops
// ---------------------------------------------------------------------------

export const GATE_LABEL: Record<ReleaseGate, string> = {
  G7: "G7 · brief",
  G8: "G8 · AI media",
  G8b: "G8b · re-review",
  H3: "H3 · legal exceptions",
};

const STOP_STATUS: Record<string, { label: string; icon: LucideIcon; ink: string }> = {
  approved: { label: "Approved", icon: CircleCheck, ink: "text-status-success" },
  decided: { label: "Decided", icon: CircleCheck, ink: "text-status-success" },
  not_required: { label: "Not required", icon: CircleMinus, ink: "text-fg-subtle" },
  rejected: { label: "Rejected", icon: CircleX, ink: "text-status-failed-ink" },
  expired: { label: "Expired", icon: CircleX, ink: "text-status-failed-ink" },
  pending: { label: "Pending", icon: CircleDashed, ink: "text-status-gate-ink" },
  required: { label: "Open", icon: CircleDashed, ink: "text-status-gate-ink" },
};

function StopStatus({ status }: { status: string }) {
  const found = STOP_STATUS[status] ?? { label: status, icon: CircleDashed, ink: "text-fg-subtle" };
  const Icon = found.icon;
  return (
    <span className="inline-flex items-center gap-1 whitespace-nowrap" data-status={status}>
      <Icon className={cn("size-3.5", found.ink)} aria-hidden />
      {found.label}
    </span>
  );
}

/** G7, G8, G8b and H3 as the package records them: decision, decider, time (law 40). */
export function StopsTable({ stops, compact = false }: { stops: ReleaseStop[]; compact?: boolean }) {
  return (
    <div className="rounded-token border" data-testid="release-stops">
      <Table label="Decisions this package records" className="min-w-0">
        <thead>
          <Tr>
            <Th>Stop</Th>
            <Th>Decision</Th>
            <Th>Decided by</Th>
            <Th className={cn(compact && "hidden sm:table-cell")}>When</Th>
          </Tr>
        </thead>
        <tbody>
          {stops.map((stop) => (
            <Tr key={stop.gate} data-testid="release-stop" data-gate={stop.gate}>
              <Td className="whitespace-nowrap font-medium">{GATE_LABEL[stop.gate]}</Td>
              <Td>
                <StopStatus status={stop.status} />
                {stop.detail && !compact ? (
                  <span className="block max-w-xs whitespace-normal text-xs text-fg-muted">{stop.detail}</span>
                ) : null}
              </Td>
              <Td className="text-fg-muted" data-testid="stop-decider">
                {stop.decided_by_name ?? (stop.decided_by ? <MonoId value={stop.decided_by} label="decider id" /> : "—")}
              </Td>
              <Td className={cn("text-fg-muted", compact && "hidden sm:table-cell")}>
                <When at={stop.decided_at} />
              </Td>
            </Tr>
          ))}
        </tbody>
      </Table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// pins
// ---------------------------------------------------------------------------

/** What the package was written against: the plan, the ruleset, the context, the constants, the catalogue. */
export function PinsList({ pins }: { pins: PackagePins }) {
  const rows: [string, ReactNode][] = [
    ["Plan", <span key="plan" className="inline-flex flex-wrap items-center gap-2 tabular-nums">v{pins.plan_version} <MonoId value={pins.plan_id} label="plan id" /></span>],
    ["Ruleset", <span key="rs" className="font-mono text-xs">{pins.ruleset_version}</span>],
    ["Creative context", <MonoId key="ctx" value={pins.context_hash} label="context hash" />],
    ["Constants", <span key="c" className="font-mono text-xs">{pins.constants_version}</span>],
    ["Model catalogue", <MonoId key="cat" value={pins.catalogue_hash} label="catalogue hash" />],
  ];
  return (
    <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2" data-testid="package-pins">
      {rows.map(([term, value]) => (
        <div key={term} className="flex min-w-0 items-baseline justify-between gap-3 border-b border-dashed pb-1.5">
          <dt className="text-fg-muted">{term}</dt>
          <dd className="min-w-0 text-right text-fg">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

// ---------------------------------------------------------------------------
// 4.7.2's thirteen checks
// ---------------------------------------------------------------------------

/**
 * `BlockingChecklist` — §11's thirteen checks as 4.7.2 ran them, the server's
 * list and the server's findings. Nothing here is re-evaluated: a check is
 * passed because 4.7.2 recorded no blocking issue for it.
 */
export function BlockingChecklist({ checklist, critique }: { checklist: ChecklistItem[]; critique: CreativeCritique | null }) {
  if (!critique || checklist.length === 0) {
    return (
      <p className="text-sm text-fg-muted" data-testid="checklist-pending">
        Node 4.7.2 has not checked this package yet. It runs the thirteen blocking checks once 4.7.1 assembles it.
      </p>
    );
  }
  const notes = critique.issues.filter((issue) => issue.severity !== "blocking");
  return (
    <div className="flex flex-col gap-4">
      <ol className="flex flex-col divide-y rounded-token border" data-testid="blocking-checklist">
        {checklist.map((item) => (
          <li
            key={item.check}
            className="flex flex-col gap-1.5 px-4 py-2.5 text-sm"
            data-testid="blocking-check"
            data-check={item.check}
            data-passed={String(item.passed)}
          >
            <p className="flex items-baseline gap-2">
              {item.passed ? (
                <CircleCheck className="size-4 shrink-0 translate-y-0.5 text-status-success" aria-hidden />
              ) : (
                <CircleX className="size-4 shrink-0 translate-y-0.5 text-status-failed-ink" aria-hidden />
              )}
              <span className={cn("text-fg", !item.passed && "font-medium")}>{item.title}</span>
              <span className="ml-auto shrink-0 text-xs text-fg-subtle tabular-nums">
                {item.check.replace("check_", "Check ")} · {item.passed ? "passed" : `${item.issues.length} blocking`}
              </span>
            </p>
            {item.issues.length > 0 ? (
              <ul className="ml-6 flex flex-col gap-1.5">
                {item.issues.map((issue, index) => (
                  <li key={`${item.check}-${index}`} className="text-fg">
                    {issue.finding}
                    {issue.fix ? <span className="block text-fg-muted">{issue.fix}</span> : null}
                    {issue.asset_ids.length > 0 ? (
                      <span className="flex flex-wrap items-center gap-2 text-xs text-fg-subtle">
                        {issue.asset_ids.slice(0, 6).map((id) => (
                          <MonoId key={id} value={id} label="asset id" />
                        ))}
                        {issue.asset_ids.length > 6 ? <span className="tabular-nums">and {issue.asset_ids.length - 6} more</span> : null}
                      </span>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : null}
          </li>
        ))}
      </ol>
      {notes.length > 0 ? (
        <div className="flex flex-col gap-2" data-testid="reader-notes">
          <h3 className="text-sm font-medium text-fg">
            Reader’s notes on brief adherence{" "}
            <span className="font-normal text-fg-muted">· advisory, never blocking</span>
          </h3>
          <ul className="flex flex-col gap-2 text-sm">
            {notes.map((issue, index) => (
              <li key={index} className="flex items-baseline gap-2">
                {issue.severity === "warning" ? (
                  <TriangleAlert className="size-3.5 shrink-0 translate-y-0.5 text-status-gate-ink" aria-hidden />
                ) : (
                  <CircleDashed className="size-3.5 shrink-0 translate-y-0.5 text-fg-subtle" aria-hidden />
                )}
                <span>
                  <span className="text-xs text-fg-subtle">{issue.section} · </span>
                  {issue.finding}
                  {issue.fix ? <span className="block text-fg-muted">{issue.fix}</span> : null}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// open dependencies
// ---------------------------------------------------------------------------

const DEPENDENCY_KIND: Record<PackageDependency["kind"], string> = {
  youtube_upload: "YouTube upload",
  landing_patch: "Landing page patch",
  inherited: "From the plan",
};

/** What must happen outside this app before (or beside) launch; `Blocks launch` where it stops one. */
export function DependenciesTable({ dependencies }: { dependencies: PackageDependency[] }) {
  if (dependencies.length === 0) {
    return <p className="text-sm text-fg-muted">Nothing outside this app is waiting. The package can load as it is.</p>;
  }
  return (
    <div className="rounded-token border">
      <Table label="Open dependencies" className="min-w-0">
        <thead>
          <Tr>
            <Th className="w-full">Task</Th>
            <Th className="hidden sm:table-cell">Owner</Th>
            <Th>Stops</Th>
          </Tr>
        </thead>
        <tbody>
          {dependencies.map((dep, index) => (
            <Tr key={`${dep.kind}-${index}`} data-testid="dependency" data-blocks={dep.blocking_for}>
              <Td className="max-w-0">
                <span className="block text-xs text-fg-subtle">{DEPENDENCY_KIND[dep.kind]}</span>
                <span className="block whitespace-normal">{dep.task}</span>
                {dep.campaign_refs.length > 0 ? (
                  <span className="block text-xs text-fg-muted">{dep.campaign_refs.join(", ")}</span>
                ) : null}
              </Td>
              <Td className="hidden text-fg-muted sm:table-cell">{dep.owner}</Td>
              <Td>
                {dep.blocking_for === "launch" ? (
                  <Badge tone="danger" data-testid="blocks-launch">
                    <CircleX className="size-3 text-status-failed-ink" aria-hidden />
                    Blocks launch
                  </Badge>
                ) : (
                  <Badge>Does not block</Badge>
                )}
              </Td>
            </Tr>
          ))}
        </tbody>
      </Table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// lint summary, cost
// ---------------------------------------------------------------------------

const LINT: Record<PackageLintVerdict, { label: string; icon: LucideIcon; ink: string }> = {
  pass: { label: "Pass", icon: CircleCheck, ink: "text-status-success" },
  pass_with_warnings: { label: "Pass with warnings", icon: TriangleAlert, ink: "text-status-gate-ink" },
  fail: { label: "Fail", icon: CircleX, ink: "text-status-failed-ink" },
  indeterminate: { label: "Indeterminate", icon: CircleDashed, ink: "text-status-gate-ink" },
  unlinted: { label: "Unlinted", icon: CircleDashed, ink: "text-fg-subtle" },
};

/** Every shipped asset's verdict at the final pin, by verdict and by rule. */
export function LintSummaryTable({ summary }: { summary: LintSummary }) {
  return (
    <div className="grid gap-4 sm:grid-cols-2" data-testid="lint-summary">
      <div className="rounded-token border">
        <Table label="Lint verdicts at the final pin" className="min-w-0">
          <thead>
            <Tr>
              <Th className="w-full">Verdict</Th>
              <Th className="text-right">Assets</Th>
            </Tr>
          </thead>
          <tbody>
            {summary.by_verdict.map((row) => {
              const { label, icon: Icon, ink } = LINT[row.verdict];
              return (
                <Tr key={row.verdict} data-verdict={row.verdict}>
                  <Td>
                    <span className="inline-flex items-center gap-1.5">
                      <Icon className={cn("size-3.5", ink)} aria-hidden />
                      {label}
                    </span>
                  </Td>
                  <Td className="text-right tabular-nums">{row.count}</Td>
                </Tr>
              );
            })}
          </tbody>
        </Table>
      </div>
      <div className="rounded-token border">
        {summary.by_rule.length === 0 ? (
          <p className="px-4 py-3 text-sm text-fg-muted">No rule fired on a shipped asset.</p>
        ) : (
          <Table label="Rules that fired" className="min-w-0">
            <thead>
              <Tr>
                <Th className="w-full">Rule</Th>
                <Th className="text-right">Assets</Th>
              </Tr>
            </thead>
            <tbody>
              {summary.by_rule.map((row) => (
                <Tr key={row.rule_id}>
                  <Td className="max-w-0 truncate font-mono text-xs" title={row.rule_id}>
                    {row.rule_id}
                  </Td>
                  <Td className="text-right tabular-nums">{row.count}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </div>
    </div>
  );
}

/** What the run spent, and the media estimate the approver saw at G7 beside what it cost. */
export function CostTable({ cost }: { cost: CostSummary }) {
  const delta = costDelta(cost.media_estimate_usd, cost.media_actual_usd);
  const sign = delta.usd > 0 ? "+" : delta.usd < 0 ? "−" : "±";
  return (
    <div className="rounded-token border" data-testid="package-cost">
      <Table label="Cost, estimate against actual" className="min-w-0">
        <thead>
          <Tr>
            <Th className="w-full">Line</Th>
            <Th className="text-right">Estimate</Th>
            <Th className="text-right">Actual</Th>
          </Tr>
        </thead>
        <tbody>
          <Tr>
            <Td>Media (images and video)</Td>
            <Td className="text-right tabular-nums">{usd(cost.media_estimate_usd)}</Td>
            <Td className="text-right tabular-nums">
              {usd(cost.media_actual_usd)}
              <span className="block text-xs text-fg-muted" data-testid="media-delta">
                {sign}
                {usd(Math.abs(delta.usd))}
                {delta.pct !== null ? ` (${sign}${Math.abs(delta.pct).toFixed(0)}%)` : ""}
              </span>
            </Td>
          </Tr>
          <Tr>
            <Td className="pl-8 text-fg-muted">Images</Td>
            <Td className="text-right text-fg-subtle">—</Td>
            <Td className="text-right tabular-nums text-fg-muted">{usd(cost.image_usd)}</Td>
          </Tr>
          <Tr>
            <Td className="pl-8 text-fg-muted">Video</Td>
            <Td className="text-right text-fg-subtle">—</Td>
            <Td className="text-right tabular-nums text-fg-muted">{usd(cost.video_usd)}</Td>
          </Tr>
          <Tr>
            <Td>Text (copy, checks, reader)</Td>
            <Td className="text-right text-fg-subtle">—</Td>
            <Td className="text-right tabular-nums">{usd(cost.text_usd)}</Td>
          </Tr>
          <Tr>
            <Td className="font-medium">Total</Td>
            <Td className="text-right text-fg-subtle">—</Td>
            <Td className="text-right font-medium tabular-nums">{usd(cost.total_usd)}</Td>
          </Tr>
        </tbody>
      </Table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// the manifest
// ---------------------------------------------------------------------------

/**
 * `PackageManifest` — the files release writes and Stage 05 is handed: a tree
 * of paths, each file's bytes and sha256 (mono, cut in the middle, copied
 * whole on click). A released package's files open from here; a draft's are
 * written only when it is released, so they are listed and not linked.
 */
export function PackageManifest({
  manifest,
  packageId,
  released,
}: {
  manifest: ManifestEntry[];
  packageId: string;
  released: boolean;
}) {
  if (manifest.length === 0) {
    return <p className="text-sm text-fg-muted">This package ships no files beside its JSON: no landing patch or media.</p>;
  }
  const tree = manifestTree(manifest);
  const rows: { node: ManifestNode; depth: number }[] = [];
  const walk = (nodes: ManifestNode[], depth: number) => {
    for (const node of nodes) {
      rows.push({ node, depth });
      if (node.kind === "dir") walk(node.children, depth + 1);
    }
  };
  walk(tree.children, 0);
  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm text-fg-muted tabular-nums" data-testid="manifest-summary">
        {tree.files} {tree.files === 1 ? "file" : "files"} · {formatBytes(tree.bytes)}
        {released ? "" : " · written when the package is released"}
      </p>
      <div className="rounded-token border">
        <Table label="Package manifest" className="min-w-0">
          <thead>
            <Tr>
              <Th className="w-full">Path</Th>
              <Th className="text-right">Bytes</Th>
              <Th>sha256</Th>
            </Tr>
          </thead>
          <tbody>
            {rows.map(({ node, depth }) =>
              node.kind === "dir" ? (
                <Tr key={`d-${node.path}`} data-testid="manifest-dir">
                  <Td className="max-w-0">
                    <span className="flex items-center gap-1.5 truncate text-fg-muted" style={{ paddingLeft: depth * 16 }}>
                      <Folder className="size-3.5 shrink-0" aria-hidden />
                      {node.name}/
                      <span className="text-xs text-fg-subtle tabular-nums">
                        {node.files} {node.files === 1 ? "file" : "files"}
                      </span>
                    </span>
                  </Td>
                  <Td className="text-right tabular-nums text-fg-muted" title={`${node.bytes} bytes`}>
                    {formatBytes(node.bytes)}
                  </Td>
                  <Td />
                </Tr>
              ) : (
                <Tr key={`f-${node.entry.path}`} data-testid="manifest-file" data-path={node.entry.path}>
                  <Td className="max-w-0">
                    <span className="flex min-w-0 items-center gap-1.5" style={{ paddingLeft: depth * 16 }}>
                      <FileText className="size-3.5 shrink-0 text-fg-subtle" aria-hidden />
                      {released ? (
                        <a
                          href={packageFileUrl(packageId, node.entry.path)}
                          className="inline-flex min-w-0 items-center gap-1 truncate font-mono text-xs text-accent hover:underline"
                          title={`Open ${node.entry.path}`}
                          target="_blank"
                          rel="noreferrer"
                        >
                          <span className="truncate">{node.name}</span>
                          <Download className="size-3 shrink-0" aria-hidden />
                          <span className="sr-only">(opens the released file)</span>
                        </a>
                      ) : (
                        <span className="truncate font-mono text-xs text-fg" title={node.entry.path}>
                          {node.name}
                        </span>
                      )}
                      <span className="hidden shrink-0 text-xs text-fg-subtle sm:inline">{node.entry.media_type}</span>
                    </span>
                  </Td>
                  <Td className="text-right tabular-nums" title={`${node.entry.bytes} bytes`}>
                    {formatBytes(node.entry.bytes)}
                  </Td>
                  <Td>
                    <MonoId value={node.entry.sha256} label={`sha256 of ${node.entry.path}`} />
                  </Td>
                </Tr>
              ),
            )}
          </tbody>
        </Table>
      </div>
    </div>
  );
}
