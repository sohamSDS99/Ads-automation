"use client";

import { ArrowLeft, CircleAlert, CircleCheck, CopyX, FileClock, Globe } from "lucide-react";
import Link from "next/link";
import { useMemo, useState, type ReactNode } from "react";

import { CharCounter } from "@/components/creative/char-counter";
import { ExtensionsPreview } from "@/components/creative/extensions-preview";
import { LeadFormTradeoffChart } from "@/components/creative/lead-form-tradeoff-chart";
import { LintChip } from "@/components/creative/lint-chip";
import { OfferBindingField } from "@/components/creative/offer-binding-field";
import { SerpPreview, type Device, type SerpLine } from "@/components/creative/serp-preview";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { SegmentedControl } from "@/components/ui/segmented";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { ApiError } from "@/lib/api";
import type {
  ClaimBoundDescriptionsOutput,
  CombinationCoherenceOutput,
  CreativeAssetItem,
  ExtraGap,
  HeadlineSpreadOutput,
  LeadFormOutput,
  LintVerdict,
  OfferAssetsOutput,
  SitelinksCalloutsSnippetsOutput,
  UrlCheckRef,
  VariantBOutput,
} from "@/lib/api/creative-runs";
import { adSlots, combination, marketLabel, position, studioAd } from "@/lib/creative/ad-studio";
import { charCount } from "@/lib/creative/char-count";
import { boundFigure, formatDate, offerEnd } from "@/lib/creative/offer-window";
import { useCreativeAssets, useCreativeBrief, useCreativeOverview, useNodeRun, usePinnedRuleSet } from "@/lib/queries";

/**
 * The Extras screen (Stage 04 PRD §15.3 `…/runs/[runId]/extras`, §15.4 F):
 * what 4.3.1–4.3.3 wrote for each campaign, drawn under the campaign's ad as
 * Google would show it, then one table per kind.
 *
 * Read-only for every role. Nothing here edits an extra, and an offer's
 * figures cannot be edited anywhere (law 35): `OfferBindingField` shows them
 * as the `OfferRecord` holds them, with the way back to the record.
 *
 * The node outputs are the record of what each node wrote and why it wrote
 * nothing (`spec_missing`, gaps, rejected sitelinks); the asset rows are the
 * truth for each asset's stored lint verdict and for the offer record behind
 * a binding. No verdict is made here.
 */
export function ExtrasScreen({ projectId, runId }: { projectId: string; runId: string }) {
  const assets = useCreativeAssets(runId);
  const brief = useCreativeBrief(runId);
  const overview = useCreativeOverview(projectId);
  const n431 = useNodeRun(runId, "4.3.1");
  const n432 = useNodeRun(runId, "4.3.2");
  const n433 = useNodeRun(runId, "4.3.3");
  const spread = useNodeRun(runId, "4.2.1");
  const descriptions = useNodeRun(runId, "4.2.2");
  const coherence = useNodeRun(runId, "4.2.3");
  const variantB = useNodeRun(runId, "4.2.4");
  const [picked, setPicked] = useState<string | null>(null);
  const [device, setDevice] = useState<Device>("desktop");

  const pins = overview.data?.runs.find((run) => run.run_id === runId)?.pins ?? [];
  const pin = pins.at(-1)?.["ruleset_version"] ?? brief.data?.brief.ruleset_ref.ruleset_version ?? null;
  const ruleset = usePinnedRuleSet(projectId, pin);
  const specs = ruleset.data?.compiled.asset_specs?.specs?.["search"];
  const limit = (kind: string): number | null => specs?.[kind]?.max_chars ?? null;

  const out = {
    extras: (n431.data?.output ?? null) as SitelinksCalloutsSnippetsOutput | null,
    offers: (n432.data?.output ?? null) as OfferAssetsOutput | null,
    leadForm: (n433.data?.output ?? null) as LeadFormOutput | null,
  };
  const copy = useMemo(
    () => ({
      spread: (spread.data?.output ?? null) as HeadlineSpreadOutput | null,
      descriptions: (descriptions.data?.output ?? null) as ClaimBoundDescriptionsOutput | null,
      coherence: (coherence.data?.output ?? null) as CombinationCoherenceOutput | null,
      variantB: (variantB.data?.output ?? null) as VariantBOutput | null,
    }),
    [spread.data, descriptions.data, coherence.data, variantB.data],
  );
  const rows = useMemo(
    () => new Map((assets.data?.items ?? []).map((row) => [row.id, row] as const)),
    [assets.data],
  );

  const campaigns = [
    ...new Set([
      ...(out.extras?.campaigns ?? []).map((c) => c.campaign_ref),
      ...(out.offers?.promotions ?? []).map((p) => p.campaign_ref),
      ...(out.offers?.prices ?? []).map((p) => p.campaign_ref),
      ...(out.leadForm?.campaigns ?? []).map((c) => c.campaign_ref),
    ]),
  ];
  const campaignRef = picked && campaigns.includes(picked) ? picked : (campaigns[0] ?? null);

  const back = (
    <Link
      href={`/projects/${projectId}/creative/runs/${runId}`}
      className="inline-flex w-fit items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
    >
      <ArrowLeft className="size-4 shrink-0" aria-hidden />
      Back to the Creative Console
    </Link>
  );

  if (assets.isPending || n431.isPending || n432.isPending || n433.isPending) {
    return (
      <div className="flex flex-col gap-6">
        {back}
        <Skeleton className="h-8 w-40" />
        <Skeleton className="h-56 w-full max-w-2xl" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  const failed = [assets, n431, n432, n433]
    .map((query) => query.error)
    .find((error) => error && !(error instanceof ApiError && error.status === 404));
  if (failed) {
    return (
      <div className="flex flex-col gap-6">
        {back}
        <Alert tone="error" title="The extras could not be loaded">
          {failed instanceof ApiError ? failed.detail : "Refresh the page to try again."}
        </Alert>
      </div>
    );
  }

  if (!out.extras && !out.offers && !out.leadForm) {
    return (
      <div className="flex flex-col gap-6">
        {back}
        <EmptyState
          icon={FileClock}
          title="No extras are written yet"
          description="Nodes 4.3.1–4.3.3 write sitelinks, callouts, snippets, offer-bound promotions and prices, and the lead form once the brief is approved at G7. They appear here as each node finishes."
          action={
            <Link href={`/projects/${projectId}/creative/runs/${runId}/brief`} className="text-sm text-accent hover:underline">
              Open the brief
            </Link>
          }
        />
      </div>
    );
  }

  const extras = out.extras?.campaigns.find((c) => c.campaign_ref === campaignRef) ?? null;
  const promotions = (out.offers?.promotions ?? []).filter((p) => p.campaign_ref === campaignRef);
  const prices = (out.offers?.prices ?? []).filter((p) => p.campaign_ref === campaignRef);
  const leadForm = out.leadForm?.campaigns.find((c) => c.campaign_ref === campaignRef) ?? null;
  const gaps = (all: ExtraGap[] | undefined, kinds: string[]) =>
    (all ?? []).filter((gap) => gap.campaign_ref === campaignRef && kinds.includes(gap.asset_type));
  const verdict = (assetId: string, fallback: LintVerdict): LintVerdict =>
    rows.get(assetId)?.lint_verdict ?? fallback;
  const specsHref = `/projects/${projectId}/guidelines/specs`;

  // The campaign's first Search ad, variant A, as the Ad Studio's first combination.
  const slot = adSlots(copy.spread, copy.variantB).find((s) => s.campaign_ref === campaignRef) ?? null;
  const ad = slot ? studioAd(slot, "A", copy, assets.data?.items ?? [], brief.data?.brief.ad_groups ?? []) : null;
  const shown = ad ? combination(ad) : null;
  const line = (asset: CreativeAssetItem): SerpLine => ({
    id: asset.id,
    label: ad ? position(ad, asset) : asset.kind,
    text: asset.text ?? "",
    surface: asset.surface,
  });
  const firstPromotion = promotions[0] ?? null;
  const preview = (
    <ExtensionsPreview
      device={device}
      sitelinks={(extras?.sitelinks ?? []).map((s) => ({ id: s.asset_id, text: s.link_text, line1: s.line1, line2: s.line2 }))}
      callouts={(extras?.callouts ?? []).map((c) => c.text)}
      snippet={extras?.snippets[0] ? { header: extras.snippets[0].header, values: extras.snippets[0].values } : null}
      promotion={
        firstPromotion
          ? {
              id: firstPromotion.asset_id,
              figure: boundFigure(firstPromotion.offer_binding) ?? "",
              text: firstPromotion.text,
              ends: (() => {
                const end = offerEnd(rows.get(firstPromotion.asset_id)?.offer ?? null, firstPromotion.offer_binding);
                return end ? formatDate(end) : null;
              })(),
            }
          : null
      }
      prices={prices.flatMap((asset) =>
        asset.items.map((item) => ({ id: item.asset_id, header: item.header, figure: boundFigure(item.offer_binding) ?? "" })),
      )}
    />
  );

  return (
    <div className="flex flex-col gap-10">
      <div className="flex flex-col gap-4">
        {back}
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="min-w-0">
            <h1 className="text-lg font-medium tracking-tight text-fg">Extras</h1>
            <p className="text-sm text-fg-muted">
              {campaignRef ?? "No campaign"}
              {extras ? ` · ${marketLabel(extras.market)} · ${extras.language}` : ""}
              {pin ? (
                <>
                  {" "}
                  · linted against ruleset <span className="font-mono text-fg">{pin}</span>
                </>
              ) : null}
            </p>
          </div>
          {campaigns.length > 1 ? (
            <SegmentedControl<string>
              label="Campaign"
              value={campaignRef ?? ""}
              onChange={setPicked}
              options={campaigns.map((ref) => ({ value: ref, label: ref }))}
            />
          ) : null}
        </div>
      </div>

      <section aria-label="Preview" className="flex min-w-0 flex-col gap-2">
        {ad && shown ? (
          <SerpPreview
            device={device}
            onDevice={setDevice}
            headlines={shown.headlines.map(line)}
            descriptions={shown.descriptions.map(line)}
            finalUrl={ad.finalUrl}
            paths={ad.paths}
            caption={`${slot?.ad_group_ref}, variant A, with every extension the run wrote for ${campaignRef}. Which of them Google shows is decided per auction.`}
            extensions={preview}
          />
        ) : (
          <Alert tone="info" title="No Search ad to preview the extensions under">
            {campaignRef} has no Search ad written by 4.2.1, so its extensions are listed below without a result preview.
          </Alert>
        )}
      </section>

      <KindSection
        id="sitelinks"
        title="Sitelinks"
        status={out.extras?.status}
        why={out.extras?.why}
        specsHref={specsHref}
        gaps={gaps(extras?.gaps, ["sitelink"])}
        empty={(extras?.sitelinks.length ?? 0) + (extras?.rejected_sitelinks.length ?? 0) === 0}
      >
        <Table label={`Sitelinks for ${campaignRef}`}>
          <thead>
            <tr>
              <Th>Link text</Th>
              <Th>Description lines</Th>
              <Th>Final URL</Th>
              <Th>URL check</Th>
              <Th>Lint</Th>
            </tr>
          </thead>
          <tbody>
            {(extras?.sitelinks ?? []).map((item) => (
              <Tr key={item.asset_id} data-testid="sitelink-row">
                <Td>
                  <div className="flex items-baseline gap-2">
                    <span>{item.link_text}</span>
                    <CharCounter count={charCount("sitelink", item.link_text)} limit={limit("sitelink")} />
                  </div>
                </Td>
                <Td className="max-w-xs text-fg-muted">
                  <p className="truncate" title={item.line1}>{item.line1}</p>
                  <p className="truncate" title={item.line2}>{item.line2}</p>
                </Td>
                <Td className="max-w-xs">
                  <FinalUrl url={item.final_url} check={item.url_check} />
                </Td>
                <Td>
                  <UrlCheckChip check={item.url_check} />
                </Td>
                <Td>
                  <LintChip verdict={verdict(item.asset_id, item.lint.verdict)} />
                </Td>
              </Tr>
            ))}
            {(extras?.rejected_sitelinks ?? []).map((item) => (
              <Tr key={`rejected-${item.final_url}-${item.link_text}`} data-testid="sitelink-rejected-row">
                <Td>
                  <div className="flex flex-col">
                    <span className="text-fg-muted line-through decoration-fg-subtle">{item.link_text}</span>
                    <span className="text-xs text-fg-muted">Not written: its URL failed the check</span>
                  </div>
                </Td>
                <Td className="text-fg-subtle">—</Td>
                <Td className="max-w-xs">
                  <FinalUrl url={item.final_url} check={item.url_check} />
                </Td>
                <Td>
                  <UrlCheckChip check={item.url_check} />
                </Td>
                <Td className="text-xs text-fg-muted">Not linted</Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      </KindSection>

      <div className="grid gap-10 xl:grid-cols-2">
        <KindSection
          id="callouts"
          title="Callouts"
          status={out.extras?.status}
          why={out.extras?.why}
          specsHref={specsHref}
          gaps={gaps(extras?.gaps, ["callout"])}
          empty={(extras?.callouts.length ?? 0) === 0}
        >
          <Table label={`Callouts for ${campaignRef}`}>
            <thead>
              <tr>
                <Th>Text</Th>
                <Th>Length</Th>
                <Th>Lint</Th>
              </tr>
            </thead>
            <tbody>
              {(extras?.callouts ?? []).map((item) => (
                <Tr key={item.asset_id}>
                  <Td>{item.text}</Td>
                  <Td>
                    <CharCounter count={charCount("callout", item.text)} limit={limit("callout")} />
                  </Td>
                  <Td>
                    <LintChip verdict={verdict(item.asset_id, item.lint.verdict)} />
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </KindSection>

        <KindSection
          id="snippets"
          title="Structured snippets"
          status={out.extras?.status}
          why={out.extras?.why}
          specsHref={specsHref}
          gaps={gaps(extras?.gaps, ["structured_snippet"])}
          empty={(extras?.snippets.length ?? 0) === 0}
        >
          <Table label={`Structured snippets for ${campaignRef}`}>
            <thead>
              <tr>
                <Th>Header</Th>
                <Th>Values</Th>
                <Th>Lint</Th>
              </tr>
            </thead>
            <tbody>
              {(extras?.snippets ?? []).map((item) => (
                <Tr key={item.asset_id}>
                  <Td>{item.header}</Td>
                  <Td className="max-w-sm">
                    <ul className="flex flex-col gap-0.5">
                      {item.values.map((value) => (
                        <li key={value} className="flex items-baseline gap-2">
                          <span>{value}</span>
                          <CharCounter count={charCount("structured_snippet", value)} limit={limit("structured_snippet")} />
                        </li>
                      ))}
                    </ul>
                  </Td>
                  <Td>
                    <LintChip verdict={verdict(item.asset_id, item.lint.verdict)} />
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </KindSection>
      </div>

      <KindSection
        id="offers"
        title="Promotions and prices"
        status={out.offers?.status}
        why={out.offers?.why}
        specsHref={specsHref}
        gaps={gaps(out.offers?.gaps, ["promotion", "price"])}
        empty={promotions.length + prices.length === 0}
        note={
          out.offers
            ? `Every figure and date is bound to an offer record and cannot be edited here. ${out.offers.offers_fresh} fresh ${out.offers.offers_fresh === 1 ? "offer" : "offers"} in the snapshot, ${out.offers.offers_stale} stale.`
            : undefined
        }
      >
        <Table label={`Promotions and prices for ${campaignRef}`} className="min-w-0">
          <thead>
            <tr>
              <Th>Asset</Th>
              <Th>Text</Th>
              <Th className="w-full">Offer</Th>
              <Th>Lint</Th>
            </tr>
          </thead>
          <tbody>
            {promotions.map((item) => (
              <Tr key={item.asset_id} data-testid="promotion-row" className="align-top">
                <Td className="align-top">
                  <Badge>Promotion</Badge>
                </Td>
                <Td className="align-top">
                  <div className="flex items-baseline gap-2">
                    <span>{item.text}</span>
                    <CharCounter count={charCount("promotion", item.text)} limit={limit("promotion")} />
                  </div>
                </Td>
                <Td className="min-w-72 align-top">
                  <OfferBindingField binding={item.offer_binding} offer={rows.get(item.asset_id)?.offer ?? null} projectId={projectId} />
                </Td>
                <Td className="align-top">
                  <LintChip verdict={verdict(item.asset_id, item.lint.verdict)} />
                </Td>
              </Tr>
            ))}
            {prices.flatMap((asset) =>
              asset.items.map((item) => (
                <Tr key={item.asset_id} data-testid="price-row" className="align-top">
                  <Td className="align-top">
                    <Badge>Price · {asset.type.replaceAll("_", " ").toLowerCase()}</Badge>
                  </Td>
                  <Td className="align-top">
                    <div className="flex items-baseline gap-2">
                      <span>{item.header}</span>
                      <CharCounter count={charCount("price", item.header)} limit={limit("price")} />
                    </div>
                    <p className="text-xs text-fg-muted">{item.description}</p>
                  </Td>
                  <Td className="min-w-72 align-top">
                    <OfferBindingField binding={item.offer_binding} offer={rows.get(item.asset_id)?.offer ?? null} projectId={projectId} />
                  </Td>
                  <Td className="align-top">
                    <LintChip verdict={verdict(item.asset_id, item.lint.verdict)} />
                  </Td>
                </Tr>
              )),
            )}
          </tbody>
        </Table>
      </KindSection>

      <KindSection
        id="lead-form"
        title="Lead form"
        status={out.leadForm?.status}
        why={out.leadForm?.why}
        specsHref={specsHref}
        gaps={gaps(leadForm?.gaps, ["lead_form"])}
        empty={!leadForm?.form}
      >
        {leadForm?.form ? (
          <div className="grid gap-6 xl:grid-cols-5">
            <div className="flex min-w-0 flex-col gap-3 xl:col-span-2">
              <dl className="grid grid-cols-3 gap-x-3 gap-y-1.5 text-sm">
                <dt className="text-fg-muted">Headline</dt>
                <dd className="col-span-2 flex flex-wrap items-baseline gap-2">
                  {leadForm.form.headline}
                  <CharCounter count={charCount("lead_form", leadForm.form.headline)} limit={limit("lead_form")} />
                </dd>
                <dt className="text-fg-muted">Description</dt>
                <dd className="col-span-2">{leadForm.form.description}</dd>
                <dt className="text-fg-muted">Call to action</dt>
                <dd className="col-span-2 font-mono text-xs">{leadForm.form.cta}</dd>
                <dt className="text-fg-muted">Privacy policy</dt>
                <dd className="col-span-2 flex flex-col gap-1">
                  <FinalUrl url={leadForm.form.privacy_policy_url} check={leadForm.form.privacy_url_check} />
                  <UrlCheckChip check={leadForm.form.privacy_url_check} />
                </dd>
                <dt className="text-fg-muted">Lint</dt>
                <dd className="col-span-2">
                  <LintChip verdict={verdict(leadForm.form.asset_id, leadForm.form.lint.verdict)} />
                </dd>
              </dl>
              <Table label="Lead form questions" className="min-w-0">
                <thead>
                  <tr>
                    <Th className="text-right">#</Th>
                    <Th>Question</Th>
                    <Th>Qualifies</Th>
                  </tr>
                </thead>
                <tbody>
                  {leadForm.form.questions.map((question, index) => (
                    <Tr key={`${question.type}-${index}`}>
                      <Td className="text-right tabular-nums text-fg-muted">{index + 1}</Td>
                      <Td>
                        {question.text ?? <span className="font-mono text-xs">{question.type}</span>}
                        {question.options.length > 0 ? (
                          <p className="text-xs text-fg-muted">{question.options.join(" · ")}</p>
                        ) : null}
                      </Td>
                      <Td className="text-fg-muted">{question.qualifies_signal ?? "Contact"}</Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            </div>
            <div className="min-w-0 xl:col-span-3">
              {leadForm.tradeoff ? (
                <LeadFormTradeoffChart tradeoff={leadForm.tradeoff} projectId={projectId} />
              ) : (
                <Alert tone="info" title="No trade-off to draw">
                  There was no CRM history to weigh the form’s length against, so the form asks every signal the lead
                  definition requires. {leadForm.gaps.map((gap) => gap.detail).join(" ")}
                </Alert>
              )}
            </div>
          </div>
        ) : null}
      </KindSection>
    </div>
  );
}

/**
 * One kind of extra: its table, or why there is none — the node's own `why`
 * when the pinned sheet has no spec for it, with the one place that fixes it.
 */
function KindSection({
  id,
  title,
  status,
  why,
  specsHref,
  gaps,
  empty,
  note,
  children,
}: {
  id: string;
  title: string;
  status: string | undefined;
  why: string | undefined;
  specsHref: string;
  gaps: ExtraGap[];
  empty: boolean;
  note?: string;
  children: ReactNode;
}) {
  const titleId = `${id}-title`;
  return (
    <section aria-labelledby={titleId} className="flex min-w-0 flex-col gap-3" data-testid={`extras-${id}`}>
      <div className="flex flex-col gap-0.5">
        <h2 id={titleId} className="text-sm font-medium text-fg">
          {title}
        </h2>
        {note ? <p className="text-xs text-fg-muted">{note}</p> : null}
      </div>
      {status === undefined ? (
        <p className="text-sm text-fg-muted">The node that writes these has not run yet.</p>
      ) : status === "spec_missing" ? (
        <div className="flex flex-col gap-1 rounded-token border border-dashed px-4 py-3 text-sm">
          <p className="text-fg">{why}</p>
          <Link href={specsHref} className="w-fit text-accent hover:underline">
            Add the spec to the asset sheet
          </Link>
        </div>
      ) : status === "not_required" ? (
        <p className="rounded-token border border-dashed px-4 py-3 text-sm text-fg-muted">{why}</p>
      ) : empty ? (
        <p className="rounded-token border border-dashed px-4 py-3 text-sm text-fg-muted">
          None written for this campaign{gaps.length > 0 ? ":" : "."}
        </p>
      ) : (
        children
      )}
      {gaps.length > 0 ? (
        <ul className="flex flex-col gap-1 text-sm" aria-label={`Why some ${title.toLowerCase()} were not written`}>
          {gaps.map((gap) => (
            <li key={`${gap.asset_type}-${gap.reason}`} className="flex items-baseline gap-2 text-fg-muted">
              <CircleAlert className="size-3.5 shrink-0 self-center text-status-gate-ink" aria-hidden />
              <span>
                <span className="font-mono text-xs text-fg">{gap.reason}</span> {gap.detail}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

const URL_STATUS: Record<UrlCheckRef["status"], string> = {
  ok: "Resolves",
  off_domain: "Off-domain",
  http_error: "HTTP error",
  unreachable: "Unreachable",
  too_many_redirects: "Too many redirects",
  duplicate: "Duplicate page",
};

/** `preview/urlcheck.py`'s answer, in words and an icon — never colour alone. */
function UrlCheckChip({ check }: { check: UrlCheckRef }) {
  const ok = check.status === "ok";
  const Icon = ok ? CircleCheck : check.status === "duplicate" ? CopyX : check.status === "off_domain" ? Globe : CircleAlert;
  return (
    <Badge tone={ok ? "neutral" : "danger"} data-url-status={check.status} className="whitespace-nowrap">
      <Icon className={ok ? "size-3 text-status-success" : "size-3 text-status-failed"} aria-hidden />
      {URL_STATUS[check.status]}
      {check.http_status !== null ? <span className="tabular-nums">· {check.http_status}</span> : null}
    </Badge>
  );
}

/** The URL as written, and where it landed when a redirect moved it. */
function FinalUrl({ url, check }: { url: string; check: UrlCheckRef }) {
  const landed = check.final_url_after_redirects;
  return (
    <div className="min-w-0 text-xs">
      <p className="truncate font-mono text-fg" title={url}>
        {url}
      </p>
      {landed && landed !== url ? (
        <p className="truncate font-mono text-fg-muted" title={landed}>
          → {landed}
        </p>
      ) : null}
    </div>
  );
}
