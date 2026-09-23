"use client";

import { ChevronRight } from "lucide-react";
import { useState } from "react";

import { AuthorityChip, SeverityChip, describeMatcher } from "@/components/guidelines/rules-panel";
import { SpecSheet } from "@/components/guidelines/spec-sheet";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import type {
  ContentGuidelinePayload,
  LexiconEntry,
  PolicyArea,
  RegisteredClaim,
  Rule,
  VoiceExample,
} from "@/lib/api/guidelines";
import { absoluteTime } from "@/lib/format";

/**
 * The five sections of the rulebook (PRD §15.3 E).
 *
 * Two rules govern every one of them and they are the reason this file is
 * longer than the payload shape would suggest:
 *
 * 1. **A missing section renders a named absence.** The draft is readable at
 *    every point in the run — halted at G5 is the state it is most often
 *    opened in — so "this has not been written yet" has to be a thing this
 *    file can say. A blank where a section should be is indistinguishable from
 *    a section that came out empty, and only one of those is fine.
 * 2. **Nothing is asserted without its authority.** Every rule carries a
 *    severity chip and an authority chip, and the `not_applicable` policy list
 *    is rendered rather than dropped, because "we checked and it does not
 *    apply to us" is the auditable half of a compliance document.
 */

export type SectionId =
  | "summary"
  | "brand"
  | "claims"
  | "policy"
  | "specs"
  | "governance"
  | "rules";

export const SECTIONS: { id: SectionId; label: string }[] = [
  { id: "summary", label: "Summary" },
  { id: "brand", label: "Brand rules" },
  { id: "claims", label: "Claims register" },
  { id: "policy", label: "Policy profile" },
  { id: "specs", label: "Asset specs" },
  { id: "governance", label: "Governance" },
  { id: "rules", label: "Every rule" },
];

function Section({
  id,
  title,
  children,
}: {
  id: SectionId;
  title: string;
  children: React.ReactNode;
}) {
  return (
    // `scroll-mt` so an anchored jump does not put the heading under the
    // sticky header that did the jumping.
    <section id={id} aria-labelledby={`${id}-heading`} className="scroll-mt-24">
      <h2
        id={`${id}-heading`}
        className="mb-3 text-[length:var(--text-lg)] font-semibold tracking-tight"
      >
        {title}
      </h2>
      {children}
    </section>
  );
}

/** What a section says when the node that writes it has not run. */
function NotYet({ what, node }: { what: string; node: string }) {
  return (
    <p className="rounded-[var(--radius)] bg-surface-sunken p-3 text-sm text-fg-muted">
      {what} is written by node <span className="font-mono text-xs">{node}</span>, which has not run
      on this version yet.
    </p>
  );
}

export function RulebookSections({ payload }: { payload: ContentGuidelinePayload }) {
  const brand = payload.brand_rules;
  const register = payload.claims_register;
  const policy = payload.policy_profile;
  const specs = payload.asset_specs;
  const governance = payload.governance;

  return (
    <div className="flex flex-col gap-10">
      <Section id="summary" title="Summary">
        {payload.executive_summary ? (
          // 65–75ch: this is the one genuinely prose block in the document.
          <p className="max-w-[70ch] text-[length:var(--text-md)] leading-relaxed text-fg">
            {payload.executive_summary}
          </p>
        ) : (
          <NotYet what="The summary" node="3.6.1" />
        )}
        {payload.critique_issues && payload.critique_issues.length > 0 ? (
          <Critique issues={payload.critique_issues} />
        ) : null}
      </Section>

      <Section id="brand" title="Brand rules">
        {brand?.voice?.voice_words?.length || brand?.lexicon ? (
          <div className="flex flex-col gap-6">
            <VoiceWords
              words={brand.voice?.voice_words ?? []}
              definitions={brand.voice?.definition_per_word ?? {}}
              dos={brand.voice?.do_examples ?? []}
              donts={brand.voice?.dont_examples ?? []}
            />
            <Lexicon
              always={brand.lexicon?.always ?? []}
              never={brand.lexicon?.never ?? []}
              conflicts={brand.lexicon?.conflicts ?? []}
            />
          </div>
        ) : (
          <NotYet what="The voice profile and lexicon" node="3.1.1 / 3.1.2" />
        )}
      </Section>

      <Section id="claims" title="Claims register">
        {register?.claims?.length ? (
          <ClaimsRegister claims={register.claims} unsupported={register.unsupported_count ?? 0} />
        ) : (
          <NotYet what="The claims register" node="3.2.1" />
        )}
      </Section>

      <Section id="policy" title="Policy profile">
        {policy?.applicable?.length || policy?.not_applicable?.length ? (
          <PolicyProfile
            applicable={policy.applicable ?? []}
            notApplicable={policy.not_applicable ?? []}
          />
        ) : (
          <NotYet what="The policy profile" node="3.3.1" />
        )}
      </Section>

      <Section id="specs" title="Asset specs">
        <SpecSheet sheet={specs?.sheet} scope={specs?.scope} />
        {specs?.launch_minimums?.length ? (
          <div className="mt-6">
            <h3 className="mb-2 text-[length:var(--text-md)] font-medium">Launch minimums</h3>
            <ul className="flex flex-col gap-2">
              {specs.launch_minimums.map((minimum, index) => (
                <li
                  key={minimum.campaign_type ?? index}
                  className="flex flex-wrap items-center gap-2 rounded-[var(--radius)] border p-3 text-sm"
                >
                  <span className="font-medium">
                    {(minimum.campaign_type ?? "unknown").replace(/_/g, " ")}
                  </span>
                  <span className="text-fg-muted">
                    {minimum.required_assets?.length ?? 0} required,{" "}
                    {minimum.optional_but_recommended?.length ?? 0} recommended
                  </span>
                  {minimum.blocking_for_launch ? (
                    <Badge tone="danger">Blocks launch</Badge>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </Section>

      <Section id="governance" title="Governance">
        {governance?.owners || governance?.review_triggers?.length ? (
          <Governance governance={governance} />
        ) : (
          <NotYet what="The sign-off matrix and review triggers" node="3.5.1" />
        )}
      </Section>

      <Section id="rules" title="Every rule">
        {payload.rules?.length ? (
          <AllRules rules={payload.rules} />
        ) : (
          <NotYet what="The compiled rule list" node="3.6.1" />
        )}
      </Section>
    </div>
  );
}

/* ------------------------------------------------------------------------ */

function Critique({ issues }: { issues: NonNullable<ContentGuidelinePayload["critique_issues"]> }) {
  // The verdict is derived from the list, never read from a label beside it:
  // a document that says "ready" above a blocking finding is how a blocked
  // rulebook circulates as an approved one.
  const blocking = issues.filter((issue) => issue.severity === "blocking");
  return (
    <div className="mt-4">
      <h3 className="mb-2 text-[length:var(--text-md)] font-medium">
        Its own review{" "}
        <span className="font-normal text-fg-muted">
          {blocking.length > 0
            ? `— ${blocking.length} blocking ${blocking.length === 1 ? "finding" : "findings"}`
            : "— nothing blocking"}
        </span>
      </h3>
      <ul className="flex flex-col gap-2">
        {issues.map((issue, index) => (
          <li key={`${issue.check}-${index}`} className="rounded-[var(--radius)] border p-3">
            <div className="flex flex-wrap items-center gap-2">
              <Badge
                tone={
                  issue.severity === "blocking"
                    ? "danger"
                    : issue.severity === "warning"
                      ? "warning"
                      : "neutral"
                }
              >
                {issue.severity}
              </Badge>
              {issue.section ? <span className="text-xs text-fg-subtle">{issue.section}</span> : null}
            </div>
            <p className="mt-1 text-sm text-fg">{issue.finding}</p>
            {issue.fix ? <p className="mt-0.5 text-sm text-fg-muted">{issue.fix}</p> : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

function VoiceWords({
  words,
  definitions,
  dos,
  donts,
}: {
  words: string[];
  definitions: Record<string, string>;
  dos: VoiceExample[];
  donts: VoiceExample[];
}) {
  if (words.length === 0) return <NotYet what="The voice profile" node="3.1.1" />;
  return (
    <div>
      <h3 className="mb-2 text-[length:var(--text-md)] font-medium">How we sound</h3>
      <div className="grid gap-3 sm:grid-cols-2">
        {words.map((word) => (
          <div key={word} className="rounded-[var(--radius)] border p-3">
            <p className="font-medium text-fg">{word}</p>
            {definitions[word] ? (
              <p className="mt-1 text-sm text-fg-muted">{definitions[word]}</p>
            ) : null}
          </div>
        ))}
      </div>

      {/* Quoted from real ads, never invented — 3.1.1 fails the node if an
          example is not in the corpus it was shown. Rendering them as quotes
          rather than as bullet text is the whole point: these are things this
          company has actually said. */}
      <div className="mt-4 grid gap-4 sm:grid-cols-2">
        <Examples title="Say it like this" examples={dos} tone="good" />
        <Examples title="Not like this" examples={donts} tone="bad" />
      </div>
    </div>
  );
}

function Examples({
  title,
  examples,
  tone,
}: {
  title: string;
  examples: VoiceExample[];
  tone: "good" | "bad";
}) {
  return (
    <div>
      <h4 className="mb-2 text-sm font-medium text-fg-muted">{title}</h4>
      {examples.length === 0 ? (
        <p className="text-sm text-fg-subtle">No examples recorded.</p>
      ) : (
        <ul className="flex flex-col gap-3">
          {examples.map((example, index) => (
            <li key={`${example.text}-${index}`}>
              <blockquote
                className={
                  tone === "good"
                    ? "border-l-2 border-[var(--status-success)] pl-3 text-sm text-fg"
                    : "border-l-2 border-[var(--status-failed)] pl-3 text-sm text-fg"
                }
              >
                {example.text}
              </blockquote>
              {example.why ? (
                <p className="mt-1 pl-3 text-xs text-fg-subtle">{example.why}</p>
              ) : null}
              {example.rewritten_as ? (
                <p className="mt-1 pl-3 text-sm text-fg-muted">
                  Instead: <span className="text-fg">{example.rewritten_as}</span>
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Lexicon({
  always,
  never,
  conflicts,
}: {
  always: LexiconEntry[];
  never: LexiconEntry[];
  conflicts: Record<string, unknown>[];
}) {
  if (always.length === 0 && never.length === 0) return null;
  return (
    <div>
      <h3 className="mb-2 text-[length:var(--text-md)] font-medium">Words</h3>
      <div className="grid gap-4 sm:grid-cols-2">
        <LexiconColumn title="Always" entries={always} />
        <LexiconColumn title="Never" entries={never} />
      </div>
      {conflicts.length > 0 ? (
        <p className="mt-3 text-sm text-fg-muted">
          {conflicts.length} {conflicts.length === 1 ? "entry" : "entries"} could not be compiled
          into a matcher and enforce nothing. They are recorded rather than dropped so the gap is
          visible.
        </p>
      ) : null}
    </div>
  );
}

function LexiconColumn({ title, entries }: { title: string; entries: LexiconEntry[] }) {
  return (
    <div>
      <h4 className="mb-2 text-sm font-medium text-fg-muted">{title}</h4>
      {entries.length === 0 ? (
        <p className="text-sm text-fg-subtle">Nothing recorded.</p>
      ) : (
        <ul className="divide-y rounded-[var(--radius)] border">
          {entries.map((entry, index) => (
            <li key={`${entry.term}-${index}`} className="flex flex-col gap-1 p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium text-fg">{entry.term}</span>
                {entry.severity ? <SeverityChip severity={entry.severity} /> : null}
                {entry.locale ? (
                  <span className="text-xs text-fg-subtle">{entry.locale}</span>
                ) : null}
              </div>
              {entry.reason ? <p className="text-sm text-fg-muted">{entry.reason}</p> : null}
              {entry.suggested_replacement ? (
                <p className="text-sm text-fg-muted">
                  Use instead: <span className="text-fg">{entry.suggested_replacement}</span>
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

const CLAIM_STATUS_ORDER = ["approved", "pending_signoff", "unsupported", "rejected", "expired"];

function ClaimsRegister({ claims, unsupported }: { claims: RegisteredClaim[]; unsupported: number }) {
  const grouped = new Map<string, RegisteredClaim[]>();
  for (const claim of claims) {
    const status = claim.status ?? "unsupported";
    grouped.set(status, [...(grouped.get(status) ?? []), claim]);
  }
  const statuses = [...grouped.keys()].sort(
    (a, b) =>
      (CLAIM_STATUS_ORDER.indexOf(a) + 1 || 99) - (CLAIM_STATUS_ORDER.indexOf(b) + 1 || 99),
  );

  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-fg-muted">
        {claims.length} {claims.length === 1 ? "claim" : "claims"}
        {unsupported > 0 ? `, ${unsupported} with no substantiation behind them` : ""}. An
        unlicensed, unsigned, rejected or expired claim all behave the same way at lint time:
        blocking.
      </p>
      {statuses.map((status) => (
        <div key={status}>
          <h3 className="mb-2 flex items-center gap-2 text-[length:var(--text-md)] font-medium">
            {status.replace(/_/g, " ")}
            <span className="text-sm font-normal text-fg-subtle">
              {grouped.get(status)?.length}
            </span>
          </h3>
          <div className="overflow-x-auto">
            <Table label={`Claims: ${status}`}>
              <thead>
                <Tr>
                  <Th>Claim</Th>
                  <Th>Type</Th>
                  <Th>Risk</Th>
                  <Th>Markets</Th>
                  <Th>Expires</Th>
                </Tr>
              </thead>
              <tbody>
                {(grouped.get(status) ?? []).map((claim) => (
                  <Tr key={claim.claim_id}>
                    <Td>
                      <span className="block max-w-prose text-fg">{claim.claim_text ?? "—"}</span>
                    </Td>
                    <Td className="text-fg-muted whitespace-nowrap">
                      {(claim.claim_type ?? "—").replace(/_/g, " ")}
                    </Td>
                    <Td className="text-fg-muted">{claim.risk_tier ?? "—"}</Td>
                    <Td className="text-fg-muted">
                      {claim.market_scope?.length ? claim.market_scope.join(", ") : "all"}
                    </Td>
                    <Td className="text-fg-muted whitespace-nowrap">
                      {claim.expires_at ? (
                        <time dateTime={claim.expires_at}>{absoluteTime(claim.expires_at)}</time>
                      ) : (
                        "—"
                      )}
                    </Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          </div>
        </div>
      ))}
    </div>
  );
}

function PolicyProfile({
  applicable,
  notApplicable,
}: {
  applicable: PolicyArea[];
  notApplicable: PolicyArea[];
}) {
  const [showNot, setShowNot] = useState(false);
  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-3 sm:grid-cols-2">
        {applicable.map((area, index) => (
          <Card key={area.area ?? index}>
            <CardHeader title={(area.area ?? "Policy area").replace(/_/g, " ")} />
            <CardBody className="flex flex-col gap-2 text-sm">
              {area.why_applicable ? <p className="text-fg-muted">{area.why_applicable}</p> : null}
              {area.obligations?.length ? (
                <ul className="list-disc pl-4 text-fg">
                  {area.obligations.map((obligation) => (
                    <li key={obligation}>{obligation}</li>
                  ))}
                </ul>
              ) : null}
              {area.policy_ref ? (
                <a
                  href={area.policy_ref}
                  target="_blank"
                  rel="noreferrer"
                  className="text-accent underline-offset-2 hover:underline"
                >
                  Google&rsquo;s wording
                </a>
              ) : null}
            </CardBody>
          </Card>
        ))}
      </div>

      {/* Collapsed but present. "We checked and it does not apply" is the part
          an auditor asks for, and a document that silently omits it cannot
          distinguish "not applicable" from "never considered". */}
      {notApplicable.length > 0 ? (
        <div className="rounded-[var(--radius)] border">
          <button
            type="button"
            onClick={() => setShowNot((current) => !current)}
            aria-expanded={showNot}
            className="flex w-full items-center gap-2 p-3 text-left text-sm font-medium text-fg-muted transition-colors hover:text-fg"
          >
            <ChevronRight
              className={showNot ? "size-4 rotate-90 transition-transform" : "size-4 transition-transform"}
              aria-hidden
            />
            {notApplicable.length} policy {notApplicable.length === 1 ? "area" : "areas"} we checked
            and ruled out
          </button>
          {showNot ? (
            <ul className="divide-y border-t">
              {notApplicable.map((area, index) => (
                <li key={area.area ?? index} className="p-3 text-sm">
                  <span className="font-medium text-fg">
                    {(area.area ?? "Policy area").replace(/_/g, " ")}
                  </span>
                  {area.why_not ? <p className="mt-0.5 text-fg-muted">{area.why_not}</p> : null}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function Governance({
  governance,
}: {
  governance: NonNullable<ContentGuidelinePayload["governance"]>;
}) {
  return (
    <div className="flex flex-col gap-4">
      {governance.owners_rationale ? (
        <p className="max-w-[70ch] text-sm text-fg-muted">{governance.owners_rationale}</p>
      ) : null}

      {governance.review_triggers?.length ? (
        <div>
          <h3 className="mb-2 text-[length:var(--text-md)] font-medium">
            What always goes to a reviewer
          </h3>
          <ul className="divide-y rounded-[var(--radius)] border">
            {governance.review_triggers.map((trigger, index) => (
              <li key={trigger.id ?? index} className="flex flex-col gap-1 p-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium text-fg">{trigger.pattern ?? "—"}</span>
                  {trigger.severity ? <SeverityChip severity={trigger.severity} /> : null}
                  {trigger.reviewer_role ? (
                    <Badge>{trigger.reviewer_role.replace(/_/g, " ")}</Badge>
                  ) : null}
                </div>
                {trigger.why ? <p className="text-fg-muted">{trigger.why}</p> : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {governance.learned_rules?.length ? (
        <div>
          <h3 className="mb-2 text-[length:var(--text-md)] font-medium">
            Rules learned from disapprovals
          </h3>
          <ul className="divide-y rounded-[var(--radius)] border">
            {governance.learned_rules.map((rule, index) => (
              <li key={rule.rule_id ?? index} className="flex flex-col gap-1 p-3 text-sm">
                <span className="font-medium text-fg">{rule.construction ?? "—"}</span>
                <span className="text-fg-muted">
                  {rule.policy_topic?.replace(/_/g, " ")}
                  {rule.occurrences ? ` · seen ${rule.occurrences}×` : ""}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

/**
 * The flattened rule list — what `compiler.compile` reads and Stage 04 runs.
 *
 * Grouped by category rather than listed flat: 800 rules in one column is a
 * list nobody finishes, and the category is the axis a reader already has in
 * their head ("what are the claim rules?").
 */
function AllRules({ rules }: { rules: Rule[] }) {
  const grouped = new Map<string, Rule[]>();
  for (const rule of rules) {
    grouped.set(rule.category, [...(grouped.get(rule.category) ?? []), rule]);
  }
  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-fg-muted">
        {rules.length} {rules.length === 1 ? "rule" : "rules"}, each with the authority behind it.
        This is the list the compiler reads — a rule that is not here cannot be enforced.
      </p>
      {[...grouped.entries()]
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([category, items]) => (
          <div key={category}>
            <h3 className="mb-2 flex items-center gap-2 text-[length:var(--text-md)] font-medium">
              {category.replace(/_/g, " ")}
              <span className="text-sm font-normal text-fg-subtle">{items.length}</span>
            </h3>
            <ul className="divide-y rounded-[var(--radius)] border">
              {items.map((rule) => (
                <li key={rule.rule_id} className="flex flex-col gap-1.5 p-3">
                  <p className="text-sm text-fg">{rule.message}</p>
                  <p className="text-sm text-fg-muted">{describeMatcher(rule.matcher)}</p>
                  <div className="flex flex-wrap items-center gap-2">
                    <SeverityChip severity={rule.severity} />
                    <AuthorityChip authority={rule.authority} />
                    <span data-numeric className="font-mono text-xs text-fg-subtle">
                      {rule.rule_id}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        ))}
    </div>
  );
}
