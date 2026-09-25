import { CircleCheck, CircleMinus, CirclePlus, OctagonX, TriangleAlert, Unplug, type LucideIcon } from "lucide-react";

import { FoldOverlay } from "@/components/creative/fold-overlay";
import { LintChip } from "@/components/creative/lint-chip";
import { PatchViewer } from "@/components/creative/patch-viewer";
import { WordDiff } from "@/components/creative/word-diff";
import { Badge } from "@/components/ui/badge";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import {
  landingScreenshotUrl,
  type FormAudit,
  type LandingAuditItem,
  type LandingAuditVerdict,
  type LandingDevice,
} from "@/lib/api/creative-runs";

const VERDICT: Record<LandingAuditVerdict, { label: string; icon: LucideIcon; tone: "neutral" | "warning" | "danger"; ink: string }> = {
  ok: { label: "OK", icon: CircleCheck, tone: "neutral", ink: "text-status-success" },
  needs_change: { label: "Needs change", icon: TriangleAlert, tone: "warning", ink: "text-status-gate-ink" },
  blocking_for_launch: { label: "Blocks launch", icon: OctagonX, tone: "danger", ink: "text-status-failed" },
  unreachable: { label: "Unreachable", icon: Unplug, tone: "danger", ink: "text-status-failed" },
};

const KEEP_REASON: Record<string, string> = {
  required_signal: "required signal",
  consent: "consent",
  privacy: "privacy",
  routing_contact: "routing contact",
};

/** The audit's verdict, as 4.5.2 recorded it: word, icon, tone — never colour alone. */
export function LandingVerdictChip({ verdict }: { verdict: LandingAuditVerdict }) {
  const { label, icon: Icon, tone, ink } = VERDICT[verdict];
  return (
    <Badge tone={tone} data-verdict={verdict} className="whitespace-nowrap">
      <Icon className={`size-3 ${ink}`} aria-hidden />
      {label}
    </Badge>
  );
}

/**
 * `LandingAuditCard` — one landing URL as 4.5.1 and 4.5.2 audited it (Stage 04
 * PRD §15.4 J). A card because it holds a decision: the verdict, top-right,
 * and the patch that would change it. Every score, box and keep/remove is the
 * nodes'; the card lays them out.
 */
export function LandingAuditCard({ audit }: { audit: LandingAuditItem }) {
  const titleId = `landing-${audit.id}-title`;
  const offerOn = (device: LandingDevice) => audit.offer_above_fold.find((item) => item.device === device) ?? null;
  const shot = (device: LandingDevice) => (audit.screenshots[device] ? landingScreenshotUrl(audit.id, device) : null);
  const redirected = audit.final_url && audit.final_url !== audit.url ? audit.final_url : null;

  return (
    <article
      aria-labelledby={titleId}
      data-testid="landing-audit-card"
      data-verdict={audit.verdict}
      className="flex min-w-0 flex-col rounded-token border bg-surface-raised"
    >
      <header className="flex items-start justify-between gap-3 border-b px-4 py-3">
        <div className="min-w-0">
          <h2 id={titleId} className="truncate font-mono text-sm text-fg" title={audit.url}>
            {audit.url}
          </h2>
          <p className="text-xs text-fg-muted">
            {audit.http_status !== null ? <span className="tabular-nums">HTTP {audit.http_status}</span> : "No answer"}
            {redirected ? <> · redirected to <span className="font-mono">{redirected}</span></> : null} ·{" "}
            {audit.ad_group_refs.length === 1 ? "ad group" : "ad groups"} {audit.ad_group_refs.join(", ")}
          </p>
        </div>
        <LandingVerdictChip verdict={audit.verdict} />
      </header>

      <div className="flex flex-col gap-6 p-4">
        {audit.reasons.length > 0 ? (
          <ul className="flex flex-col gap-1 text-sm text-fg" aria-label="Why the verdict is what it is">
            {audit.reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        ) : null}

        <div className="flex flex-col gap-4 md:flex-row md:items-start">
          <FoldOverlay
            device="mobile"
            src={shot("mobile")}
            fold={audit.fold_px.mobile}
            offer={offerOn("mobile")}
            obscured={audit.obscured_by_overlay.mobile}
            className="md:w-56 md:shrink-0"
          />
          <FoldOverlay
            device="desktop"
            src={shot("desktop")}
            fold={audit.fold_px.desktop}
            offer={offerOn("desktop")}
            obscured={audit.obscured_by_overlay.desktop}
            className="md:flex-1"
          />
        </div>

        <MessageMatchBlock audit={audit} />

        <FormTable form={audit.form} />

        {audit.has_patch ? <PatchViewer auditId={audit.id} url={audit.final_url ?? audit.url} /> : null}
      </div>
    </article>
  );
}

function MessageMatchBlock({ audit }: { audit: LandingAuditItem }) {
  const match = audit.message_match;
  const titleId = `match-${audit.id}-title`;
  let body;
  if (!match || match.verdict === "unavailable" || match.score === null) {
    body = <p className="text-sm text-fg-muted">The page never rendered, so there is no H1 to match against the ads.</p>;
  } else {
    // The page's score is its weakest ad group on its weakest device (4.5.1).
    const weakest = match.scores.find((s) => s.score === match.score) ?? null;
    const groups = new Set(match.scores.map((s) => s.ad_group_ref)).size;
    body =
      weakest && weakest.best_headline ? (
        <WordDiff
          headline={weakest.best_headline}
          h1={audit.h1[weakest.device]}
          score={match.score}
          threshold={match.threshold}
          verdict={match.verdict}
          caption={`${weakest.ad_group_ref} on ${weakest.device} — the weakest of ${groups} ${groups === 1 ? "ad group" : "ad groups"} on two devices`}
        />
      ) : (
        <p className="text-sm text-fg-muted">
          No headline of {weakest?.ad_group_ref ?? "the ad group"} shares a word with the page’s H1 (match{" "}
          <span className="font-mono tabular-nums">{match.score.toFixed(2)}</span> against{" "}
          <span className="font-mono tabular-nums">{match.threshold.toFixed(2)}</span>).
        </p>
      );
  }
  return (
    <section aria-labelledby={titleId} className="flex min-w-0 flex-col gap-3">
      <h3 id={titleId} className="text-sm font-medium text-fg">
        Message match
      </h3>
      {body}
      {audit.proposed_h1 ? (
        <div className="flex flex-col gap-1 rounded-token border px-3 py-2">
          <p className="text-xs text-fg-muted">Proposed H1</p>
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <p className="text-sm text-fg">{audit.proposed_h1.text}</p>
            <p className="font-mono text-xs text-fg-muted tabular-nums">
              match {audit.proposed_h1.score.toFixed(2)}
            </p>
            <LintChip verdict={audit.proposed_h1.lint.verdict} />
          </div>
        </div>
      ) : audit.proposed_h1_note ? (
        <p className="text-sm text-fg-muted">{audit.proposed_h1_note}</p>
      ) : null}
    </section>
  );
}

function FormTable({ form }: { form: FormAudit | null }) {
  if (!form) {
    return (
      <section className="flex flex-col gap-2">
        <h3 className="text-sm font-medium text-fg">Form</h3>
        <p className="text-sm text-fg-muted">The page has no form a visitor fills in.</p>
      </section>
    );
  }
  const keep = new Set(form.minimal_set);
  const reasons = new Map(form.keep_reason.map((reason) => [reason.field, reason] as const));
  return (
    <section className="flex min-w-0 flex-col gap-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4">
        <h3 className="text-sm font-medium text-fg">Form</h3>
        <p className="text-xs text-fg-muted tabular-nums">
          {form.fields.length} fields · keep {form.minimal_set.length} · remove {form.remove.length}
        </p>
      </div>
      <Table label="Form fields: keep or remove, with the lead signal each carries">
        <thead>
          <tr>
            <Th>Field</Th>
            <Th>Type</Th>
            <Th>Required</Th>
            <Th>Lead signal</Th>
            <Th>Change</Th>
          </tr>
        </thead>
        <tbody>
          {form.fields.map((field) => {
            const kept = keep.has(field.name);
            const reason = reasons.get(field.name);
            return (
              <Tr key={field.name} data-testid="form-field-row" data-decision={kept ? "keep" : "remove"}>
                <Td>
                  <span className="font-mono text-xs">{field.name}</span>
                  {field.label ? <p className="max-w-xs truncate text-xs text-fg-muted">{field.label}</p> : null}
                </Td>
                <Td className="font-mono text-xs text-fg-muted">{field.type}</Td>
                <Td className="text-fg-muted">{field.required ? "Yes" : "No"}</Td>
                <Td className="text-fg-muted">{field.mapped_signal ?? "—"}</Td>
                <Td>
                  {kept ? (
                    <Badge className="whitespace-nowrap">
                      <CirclePlus className="size-3 text-status-success" aria-hidden />
                      Keep{reason ? ` · ${KEEP_REASON[reason.reason] ?? reason.reason}` : ""}
                    </Badge>
                  ) : (
                    <Badge tone="danger" className="whitespace-nowrap">
                      <CircleMinus className="size-3 text-status-failed" aria-hidden />
                      Remove
                    </Badge>
                  )}
                </Td>
              </Tr>
            );
          })}
        </tbody>
      </Table>
      {form.missing_signals.length > 0 ? (
        <p className="text-sm text-fg-muted">
          No field asks for: <span className="text-fg">{form.missing_signals.join(", ")}</span>.
        </p>
      ) : null}
    </section>
  );
}
