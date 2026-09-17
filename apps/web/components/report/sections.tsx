"use client";

import { ClaimLine, CiteGroup } from "@/components/report/claim";
import {
  KeywordChart,
  MessageChart,
  SeasonalityChart,
  SpendChart,
} from "@/components/report/charts";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import type { Citations } from "@/lib/citations";
import { INTENT_LABEL, VERDICT_LABEL, type ResearchReport } from "@/lib/api/reports";
import { compactNumber, usd } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * The report itself (PRD §11, §13.4 C).
 *
 * One section per part of the contract, in the contract's own order, so the
 * screen and the PDF are the same document read two ways. Sections render even
 * when they are empty: this is an audit artifact, and a missing competitive
 * landscape is a finding, not a reason to hide the heading.
 */
export type SectionSpec = { id: string; label: string };

export const SECTIONS: SectionSpec[] = [
  { id: "summary", label: "Summary" },
  { id: "business", label: "Our business" },
  { id: "account", label: "What we already ran" },
  { id: "competition", label: "The competition" },
  { id: "demand", label: "The demand" },
  { id: "readiness", label: "Are we ready" },
  { id: "keywords", label: "Priced keywords" },
  { id: "actions", label: "What to do next" },
  { id: "questions", label: "Open questions" },
];

type Props = { report: ResearchReport; citations: Citations; projectId: string };

export function Section({
  id,
  title,
  children,
  className,
}: {
  id: string;
  title: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section id={id} className={cn("scroll-mt-24", className)}>
      <h2 className="mb-3 text-[length:var(--text-lg)] font-semibold tracking-tight">{title}</h2>
      <div className="space-y-4">{children}</div>
    </section>
  );
}

function Nothing({ children }: { children: React.ReactNode }) {
  return <p className="text-sm text-fg-subtle">{children}</p>;
}

function SubHeading({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="text-xs font-medium tracking-wide text-fg-muted uppercase">{children}</h3>
  );
}

export function SummarySection({ report, citations, projectId }: Props) {
  const blockers = report.launch_blockers ?? [];
  return (
    <Section id="summary" title="Summary">
      <p className="max-w-[68ch] text-[length:var(--text-md)] leading-relaxed text-fg">
        {report.executive_summary}
      </p>

      <div>
        <SubHeading>What stands in the way</SubHeading>
        {blockers.length === 0 ? (
          <Nothing>Nothing is blocking a launch.</Nothing>
        ) : (
          <ul className="mt-2 space-y-2">
            {blockers.map((claim, index) => (
              <li key={index} className="flex gap-2 text-sm">
                <span aria-hidden className="mt-1.5 size-1.5 shrink-0 rounded-full bg-status-failed" />
                <ClaimLine claim={claim} citations={citations} projectId={projectId} />
              </li>
            ))}
          </ul>
        )}
      </div>
    </Section>
  );
}

export function BusinessSection({ report, citations, projectId }: Props) {
  const context = report.business_context ?? {};
  const products = context.products ?? [];
  const segments = context.segments ?? [];
  const markets = context.markets ?? [];
  const compliance = context.compliance;

  return (
    <Section id="business" title="Our business">
      <div className="grid gap-3 sm:grid-cols-3">
        <Figure label="Lifetime value" value={context.ltv_estimate} />
        <Figure label="Target cost per customer" value={context.target_cac} />
        <Figure
          label="Payback"
          value={context.payback_months}
          format={(value) => `${value.toFixed(1)} months`}
        />
      </div>

      <div>
        <SubHeading>Products</SubHeading>
        {products.length === 0 ? (
          <Nothing>No products recorded.</Nothing>
        ) : (
          <Table label="Products" className="mt-2">
            <thead>
              <tr>
                <Th>Product</Th>
                <Th>Pricing</Th>
                <Th className="text-right">Contract value</Th>
                <Th className="text-right">Margin</Th>
              </tr>
            </thead>
            <tbody>
              {products.map((product) => (
                <Tr key={product.name}>
                  <Td>{product.name}</Td>
                  <Td className="text-fg-muted">{product.price_model ?? "—"}</Td>
                  <Td data-numeric className="text-right">
                    {product.acv === null || product.acv === undefined ? "—" : usd(product.acv, 0)}
                  </Td>
                  <Td data-numeric className="text-right">
                    {product.gross_margin_pct === null || product.gross_margin_pct === undefined
                      ? "—"
                      : `${product.gross_margin_pct.toFixed(0)}%`}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </div>

      <div>
        <SubHeading>Who we sell to</SubHeading>
        {segments.length === 0 ? (
          <Nothing>No segments recorded.</Nothing>
        ) : (
          <ul className="mt-2 grid gap-2 sm:grid-cols-2">
            {segments.map((segment) => (
              <li key={segment.label} className="rounded-[var(--radius)] border bg-surface p-3">
                <p className="text-sm font-medium text-fg">
                  {segment.label}
                  <CiteGroup
                    ids={segment.evidence_ids}
                    citations={citations}
                    projectId={projectId}
                  />
                </p>
                <p className="mt-1 text-sm text-fg-muted">{firmographics(segment.firmographics)}</p>
                {segment.share_of_revenue_pct !== null &&
                segment.share_of_revenue_pct !== undefined ? (
                  <p data-numeric className="mt-1 text-xs text-fg-subtle">
                    {segment.share_of_revenue_pct.toFixed(0)}% of revenue
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </div>

      {markets.length > 0 ? (
        <div>
          <SubHeading>Markets</SubHeading>
          <p className="mt-2 text-sm text-fg-muted">
            {markets
              .map((market) =>
                [market.country, market.language, market.currency].filter(Boolean).join(" · "),
              )
              .join(" • ")}
          </p>
        </div>
      ) : null}

      {compliance ? (
        <div>
          <SubHeading>What we may not say</SubHeading>
          <div className="mt-2 grid gap-3 sm:grid-cols-2">
            <List title="Prohibited claims" items={compliance.prohibited_claims} />
            <List title="Required disclaimers" items={compliance.required_disclaimers} />
          </div>
          {compliance.regulated_terms && compliance.regulated_terms.length > 0 ? (
            <ul className="mt-2 space-y-1 text-sm text-fg-muted">
              {compliance.regulated_terms.map((term) => (
                <li key={term.term}>
                  <span className="font-medium text-fg">{term.term}</span> — {term.rule}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </Section>
  );
}

export function AccountSection({ report, citations, projectId }: Props) {
  const learnings = report.account_learnings ?? {};
  const winners = learnings.winners ?? [];
  const losers = learnings.losers ?? [];
  const profitable = learnings.profitable_terms ?? [];
  const wasteful = learnings.wasteful_terms ?? [];

  return (
    <Section id="account" title="What we already ran">
      <SpendChart profitable={profitable} wasteful={wasteful} />

      <div className="grid gap-4 md:grid-cols-2">
        <Findings title="What worked" findings={winners} citations={citations} projectId={projectId} />
        <Findings title="What did not" findings={losers} citations={citations} projectId={projectId} />
      </div>

      {learnings.structural_findings && learnings.structural_findings.length > 0 ? (
        <div>
          <SubHeading>About the account itself</SubHeading>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-fg-muted">
            {learnings.structural_findings.map((finding) => (
              <li key={finding}>{finding}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {wasteful.length > 0 ? (
        <div>
          <SubHeading>Money going nowhere</SubHeading>
          <Table label="Wasteful search terms" className="mt-2">
            <thead>
              <tr>
                <Th>Search term</Th>
                <Th className="text-right">Cost</Th>
                <Th>What to do</Th>
              </tr>
            </thead>
            <tbody>
              {wasteful.slice(0, 20).map((term) => (
                <Tr key={term.term}>
                  <Td>
                    {term.term}
                    <CiteGroup ids={term.evidence_ids} citations={citations} projectId={projectId} />
                  </Td>
                  <Td data-numeric className="text-right">
                    {term.cost === null || term.cost === undefined ? "—" : usd(term.cost, 0)}
                  </Td>
                  <Td className="text-fg-muted">{term.recommended_action ?? "—"}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </div>
      ) : null}

      {learnings.tried_and_failed && learnings.tried_and_failed.length > 0 ? (
        <div>
          <SubHeading>Tried before, do not repeat</SubHeading>
          <ul className="mt-2 space-y-2 text-sm">
            {learnings.tried_and_failed.map((experiment) => (
              <li key={experiment.what} className="rounded-[var(--radius)] border bg-surface p-3">
                <p className="font-medium text-fg">{experiment.what}</p>
                <p className="mt-1 text-fg-muted">
                  {[experiment.when, experiment.outcome, experiment.do_not_repeat_reason]
                    .filter(Boolean)
                    .join(" · ")}
                </p>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {winners.length === 0 && losers.length === 0 && profitable.length === 0 ? (
        <Nothing>No account history was available to this run.</Nothing>
      ) : null}
    </Section>
  );
}

export function CompetitionSection({ report, citations, projectId }: Props) {
  const landscape = report.competitive_landscape ?? {};
  const competitors = landscape.competitors ?? [];
  const clusters = landscape.message_clusters ?? [];
  const whitespace = landscape.whitespace ?? [];

  return (
    <Section id="competition" title="The competition">
      {landscape.recommended_claim ? (
        <div className="rounded-[var(--radius)] border border-accent/40 bg-accent-soft p-3">
          <SubHeading>The claim we should make</SubHeading>
          <p className="mt-1 text-[length:var(--text-md)] text-fg">{landscape.recommended_claim}</p>
          {landscape.substantiation_required && landscape.substantiation_required.length > 0 ? (
            <p className="mt-2 text-xs text-fg-muted">
              Needs substantiating: {landscape.substantiation_required.join(", ")}
            </p>
          ) : null}
        </div>
      ) : null}

      <MessageChart clusters={clusters} />

      <div>
        <SubHeading>Who we are up against</SubHeading>
        {competitors.length === 0 ? (
          <Nothing>No competitors were identified.</Nothing>
        ) : (
          <Table label="Competitors" className="mt-2">
            <thead>
              <tr>
                <Th>Competitor</Th>
                <Th>Overlaps on</Th>
                <Th className="text-right">Overlap</Th>
              </tr>
            </thead>
            <tbody>
              {competitors.map((competitor) => (
                <Tr key={competitor.domain}>
                  <Td>
                    <span className="text-fg">{competitor.name ?? competitor.domain}</span>
                    <span className="ml-2 font-mono text-xs text-fg-subtle">
                      {competitor.domain}
                    </span>
                    <CiteGroup
                      ids={competitor.evidence_ids}
                      citations={citations}
                      projectId={projectId}
                    />
                  </Td>
                  <Td className="text-fg-muted">{(competitor.overlap_basis ?? []).join(", ") || "—"}</Td>
                  <Td data-numeric className="text-right text-fg-muted">
                    {competitor.overlap_score === null || competitor.overlap_score === undefined
                      ? "—"
                      : competitor.overlap_score.toFixed(2)}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </div>

      {whitespace.length > 0 ? (
        <div>
          <SubHeading>What nobody is saying</SubHeading>
          <ul className="mt-2 space-y-2">
            {whitespace.map((gap) => (
              <li key={gap.claim} className="rounded-[var(--radius)] border bg-surface p-3 text-sm">
                <p className="font-medium text-fg">
                  {gap.claim}
                  <CiteGroup ids={gap.evidence_ids} citations={citations} projectId={projectId} />
                </p>
                {gap.why_unsaid ? <p className="mt-1 text-fg-muted">{gap.why_unsaid}</p> : null}
                {gap.our_proof ? (
                  <p className="mt-1 text-fg-muted">Our proof: {gap.our_proof}</p>
                ) : null}
                {gap.risk ? <p className="mt-1 text-status-gate">Risk: {gap.risk}</p> : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Section>
  );
}

export function DemandSection({ report, citations, projectId }: Props) {
  const demand = report.demand_map ?? {};
  const mapping = demand.mapping ?? [];
  const negatives = demand.negatives ?? [];
  const gaps = demand.content_gaps ?? [];
  const keywords = report.priced_keyword_list ?? [];

  return (
    <Section id="demand" title="The demand">
      <p className="text-sm text-fg-muted">
        <span data-numeric className="text-fg">
          {compactNumber(demand.total_keywords ?? keywords.length)}
        </span>{" "}
        keywords priced and classified.
      </p>

      <KeywordChart keywords={keywords} />
      <SeasonalityChart keywords={keywords} />

      {mapping.length > 0 ? (
        <div>
          <SubHeading>Where each cluster should land</SubHeading>
          <Table label="Keyword to page mapping" className="mt-2">
            <thead>
              <tr>
                <Th>Cluster</Th>
                <Th>Best page</Th>
                <Th>Verdict</Th>
              </tr>
            </thead>
            <tbody>
              {mapping.slice(0, 25).map((row) => (
                <Tr key={row.term_cluster}>
                  <Td>{row.term_cluster}</Td>
                  <Td className="max-w-80 truncate">
                    {row.best_url ? (
                      <a
                        href={row.best_url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-accent hover:underline"
                      >
                        {row.best_url}
                      </a>
                    ) : (
                      <span className="text-fg-subtle">nothing yet</span>
                    )}
                  </Td>
                  <Td className="text-fg-muted">{VERDICT_LABEL[row.verdict] ?? row.verdict}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </div>
      ) : null}

      <div className="grid gap-4 md:grid-cols-2">
        {negatives.length > 0 ? (
          <div>
            <SubHeading>Never pay for these</SubHeading>
            <ul className="mt-2 flex flex-wrap gap-1.5">
              {negatives.slice(0, 40).map((negative) => (
                <li
                  key={`${negative.term}-${negative.match_type}`}
                  title={negative.reason ?? undefined}
                  className="rounded-full border px-2 py-0.5 font-mono text-xs text-fg-muted"
                >
                  {negative.term}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {gaps.length > 0 ? (
          <div>
            <SubHeading>Pages we do not have</SubHeading>
            <ul className="mt-2 space-y-1 text-sm text-fg-muted">
              {gaps.map((gap) => (
                <li key={gap.cluster}>
                  <span className="text-fg">{gap.cluster}</span> — {gap.required_page_type}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>

      {mapping.length === 0 && keywords.length === 0 ? (
        <Nothing>This run gathered no keyword data.</Nothing>
      ) : null}
      <CitationsNote citations={citations} projectId={projectId} />
    </Section>
  );
}

export function ReadinessSection({ report, citations, projectId }: Props) {
  const readiness = report.readiness ?? {};
  const pages = readiness.pages ?? [];
  const actions = readiness.conversion_actions ?? [];
  const lists = readiness.lists ?? [];
  const scenarios = readiness.scenarios ?? [];
  const probe = readiness.synthetic_check;

  return (
    <Section id="readiness" title="Are we ready">
      {readiness.alerts && readiness.alerts.length > 0 ? (
        <ul className="space-y-1">
          {readiness.alerts.map((alert) => (
            <li key={alert} className="flex gap-2 text-sm text-fg">
              <span aria-hidden className="mt-1.5 size-1.5 shrink-0 rounded-full bg-status-gate" />
              {alert}
            </li>
          ))}
        </ul>
      ) : null}

      {pages.length > 0 ? (
        <div>
          <SubHeading>Landing pages</SubHeading>
          <Table label="Landing page audit" className="mt-2">
            <thead>
              <tr>
                <Th>Page</Th>
                <Th className="text-right">LCP</Th>
                <Th>Issues</Th>
                <Th>Severity</Th>
              </tr>
            </thead>
            <tbody>
              {pages.map((page) => (
                <Tr key={page.url}>
                  <Td className="max-w-80 truncate">
                    <a
                      href={page.url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-accent hover:underline"
                    >
                      {page.url}
                    </a>
                    <CiteGroup
                      ids={page.evidence_ids}
                      citations={citations}
                      projectId={projectId}
                    />
                  </Td>
                  <Td data-numeric className="text-right text-fg-muted">
                    {page.lcp_ms ? `${(page.lcp_ms / 1000).toFixed(1)}s` : "—"}
                  </Td>
                  <Td className="text-fg-muted">{(page.issues ?? []).join("; ") || "—"}</Td>
                  <Td className="capitalize">{page.severity ?? "—"}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </div>
      ) : null}

      <div className="grid gap-4 md:grid-cols-2">
        {actions.length > 0 ? (
          <div>
            <SubHeading>Conversion tracking</SubHeading>
            <ul className="mt-2 space-y-1 text-sm">
              {actions.map((action) => (
                <li key={action.name} className="flex items-baseline justify-between gap-3">
                  <span className="text-fg">{action.name}</span>
                  <span className="text-fg-muted">
                    {action.status ?? "unknown"}
                    {action.staleness_days !== null && action.staleness_days !== undefined
                      ? ` · ${action.staleness_days}d stale`
                      : ""}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {probe ? (
          <div>
            <SubHeading>Synthetic conversion probe</SubHeading>
            <p className="mt-2 text-sm text-fg-muted">
              {probe.verdict === "pass"
                ? "A click we fired ourselves came back through the Ads API."
                : probe.verdict === "fail"
                  ? "A click we fired ourselves never came back — tracking is broken."
                  : "The probe was inconclusive."}
              {probe.latency_min !== null && probe.latency_min !== undefined
                ? ` Round trip ${probe.latency_min.toFixed(0)} minutes.`
                : ""}
            </p>
          </div>
        ) : null}
      </div>

      {lists.length > 0 ? (
        <div>
          <SubHeading>Audience lists</SubHeading>
          <ul className="mt-2 space-y-1 text-sm">
            {lists.map((list) => (
              <li key={list.name} className="flex items-baseline justify-between gap-3">
                <span className="text-fg">
                  {list.name}
                  <CiteGroup ids={list.evidence_ids} citations={citations} projectId={projectId} />
                </span>
                <span className={list.usable ? "text-fg-muted" : "text-status-failed"}>
                  {list.usable ? (list.consent_basis ?? "usable") : (list.blocker ?? "not usable")}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {scenarios.length > 0 ? (
        <div>
          <SubHeading>What a budget would buy</SubHeading>
          <Table label="Budget scenarios" className="mt-2">
            <thead>
              <tr>
                <Th className="text-right">Monthly budget</Th>
                <Th className="text-right">Clicks</Th>
                <Th className="text-right">Conversions</Th>
                <Th className="text-right">Cost each</Th>
                <Th className="text-right">Revenue</Th>
              </tr>
            </thead>
            <tbody>
              {scenarios.map((scenario) => (
                <Tr key={scenario.budget_usd_month}>
                  <Td data-numeric className="text-right">
                    {usd(scenario.budget_usd_month, 0)}
                  </Td>
                  <Td data-numeric className="text-right text-fg-muted">
                    {compactNumber(scenario.est_clicks ?? null)}
                  </Td>
                  <Td data-numeric className="text-right text-fg-muted">
                    {compactNumber(scenario.est_conv ?? null)}
                  </Td>
                  <Td data-numeric className="text-right text-fg-muted">
                    {scenario.est_cpa === null || scenario.est_cpa === undefined
                      ? "—"
                      : usd(scenario.est_cpa, 0)}
                  </Td>
                  <Td data-numeric className="text-right text-fg-muted">
                    {scenario.est_revenue === null || scenario.est_revenue === undefined
                      ? "—"
                      : usd(scenario.est_revenue, 0)}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </div>
      ) : null}

      {pages.length === 0 && actions.length === 0 && lists.length === 0 && scenarios.length === 0 ? (
        <Nothing>This run did not reach the readiness checks.</Nothing>
      ) : null}
    </Section>
  );
}

export function KeywordsSection({ report, citations, projectId }: Props) {
  const keywords = report.priced_keyword_list ?? [];
  const shown = [...keywords]
    .sort((a, b) => (b.volume ?? 0) - (a.volume ?? 0))
    .slice(0, 50);

  return (
    <Section id="keywords" title="Priced keywords">
      {keywords.length === 0 ? (
        <Nothing>No keyword list was produced.</Nothing>
      ) : (
        <>
          <p className="text-sm text-fg-muted">
            The {shown.length} largest by volume. The Keywords CSV export carries all{" "}
            <span data-numeric>{keywords.length.toLocaleString()}</span>, ready for Google Ads
            Editor.
          </p>
          <Table label="Priced keywords">
            <thead>
              <tr>
                <Th>Term</Th>
                <Th>Intent</Th>
                <Th className="text-right">Searches</Th>
                <Th className="text-right">CPC</Th>
                <Th>Page</Th>
              </tr>
            </thead>
            <tbody>
              {shown.map((keyword) => (
                <Tr key={`${keyword.term}-${keyword.market ?? ""}`}>
                  <Td>
                    <span className="font-mono text-xs text-fg">{keyword.term}</span>
                    <CiteGroup
                      ids={keyword.evidence_ids}
                      citations={citations}
                      projectId={projectId}
                    />
                  </Td>
                  <Td className="text-fg-muted">
                    {keyword.intent ? (INTENT_LABEL[keyword.intent] ?? keyword.intent) : "—"}
                  </Td>
                  <Td data-numeric className="text-right text-fg-muted">
                    {compactNumber(keyword.volume ?? null)}
                  </Td>
                  <Td data-numeric className="text-right text-fg-muted">
                    {keyword.cpc_low === null || keyword.cpc_low === undefined
                      ? "—"
                      : `${usd(keyword.cpc_low)}–${usd(keyword.cpc_high ?? keyword.cpc_low)}`}
                  </Td>
                  <Td className="max-w-64 truncate text-fg-muted">
                    {keyword.best_url ?? <span className="text-fg-subtle">unmapped</span>}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </>
      )}
    </Section>
  );
}

export function ActionsSection({ report, citations, projectId }: Props) {
  const actions = report.recommended_next_actions ?? [];
  return (
    <Section id="actions" title="What to do next">
      {actions.length === 0 ? (
        <Nothing>No next actions were recommended.</Nothing>
      ) : (
        <ol className="space-y-2">
          {actions.map((claim, index) => (
            <li key={index} className="flex gap-3 text-sm">
              <span data-numeric className="mt-0.5 shrink-0 font-mono text-xs text-fg-subtle">
                {String(index + 1).padStart(2, "0")}
              </span>
              <ClaimLine claim={claim} citations={citations} projectId={projectId} />
            </li>
          ))}
        </ol>
      )}
    </Section>
  );
}

export function QuestionsSection({ report }: Props) {
  const questions = report.open_questions ?? [];
  return (
    <Section id="questions" title="Open questions">
      {questions.length === 0 ? (
        <Nothing>Nothing was left open.</Nothing>
      ) : (
        <ul className="list-disc space-y-1 pl-5 text-sm text-fg-muted">
          {questions.map((question) => (
            <li key={question}>{question}</li>
          ))}
        </ul>
      )}
    </Section>
  );
}

function Findings({
  title,
  findings,
  citations,
  projectId,
}: {
  title: string;
  findings: { campaign: string; metric_delta?: string | null; period?: string | null; evidence_ids?: string[] }[];
  citations: Citations;
  projectId: string;
}) {
  return (
    <div>
      <SubHeading>{title}</SubHeading>
      {findings.length === 0 ? (
        <Nothing>Nothing recorded.</Nothing>
      ) : (
        <ul className="mt-2 space-y-1.5 text-sm">
          {findings.map((finding, index) => (
            <li key={`${finding.campaign}-${index}`}>
              <span className="text-fg">{finding.campaign}</span>
              {finding.metric_delta ? (
                <span className="text-fg-muted"> — {finding.metric_delta}</span>
              ) : null}
              {finding.period ? (
                <span className="text-fg-subtle"> ({finding.period})</span>
              ) : null}
              <CiteGroup ids={finding.evidence_ids} citations={citations} projectId={projectId} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function List({ title, items }: { title: string; items: string[] | undefined }) {
  if (!items || items.length === 0) return null;
  return (
    <div>
      <p className="text-xs text-fg-subtle">{title}</p>
      <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-fg-muted">
        {items.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

function Figure({
  label,
  value,
  format = (value: number) => usd(value, 0),
}: {
  label: string;
  value: number | null | undefined;
  format?: (value: number) => string;
}) {
  return (
    <div className="rounded-[var(--radius)] border bg-surface px-3 py-2.5">
      <p className="text-xs text-fg-subtle">{label}</p>
      <p data-numeric className="mt-0.5 text-[length:var(--text-lg)] text-fg">
        {value === null || value === undefined ? "—" : format(value)}
      </p>
    </div>
  );
}

/** A quiet note when citations are still arriving, so chips are not just dead. */
function CitationsNote({ citations, projectId }: { citations: Citations; projectId: string }) {
  void projectId;
  if (!citations.loading) return null;
  return <p className="text-xs text-fg-subtle">Resolving citations…</p>;
}

/** 1.1.2 asks the model for a sentence; the contract also accepts a map. */
function firmographics(value: string | Record<string, unknown> | null | undefined): string {
  if (!value) return "No firmographics recorded.";
  if (typeof value === "string") return value;
  return Object.entries(value)
    .map(([key, entry]) => `${key.replace(/_/g, " ")}: ${String(entry)}`)
    .join(" · ");
}
