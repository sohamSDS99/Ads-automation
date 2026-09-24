"use client";

import { ExternalLink, TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";

import { MonoId } from "@/components/creative/mono-id";
import { EvidenceIdsChip, SourceChip } from "@/components/creative/source-chip";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import type {
  AdGroupBrief,
  BriefEdits,
  BriefLine,
  ClaimRef,
  CreativeBriefView,
  OfferBinding,
  ProductDepiction,
} from "@/lib/api/creative-runs";
import { usd } from "@/lib/format";
import { cn } from "@/lib/utils";

/** A counter turns amber at nine tenths of its limit (§15.2 rule 6). */
const NEAR_LIMIT = 0.9;

const DEPICTION: Record<ProductDepiction, string> = {
  reference_guided: "Generated from the attested product references",
  composited_real: "The real product, composited in after generation",
  none: "No product in frame",
};

const CLAIM_STATUS: Record<ClaimRef["status"], string> = {
  draft: "draft",
  approved: "approved",
  rejected: "rejected",
  expired: "expired",
};

/** `12 Oct 2026` — the brief's own date shape (§15.4 D). */
function day(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

/**
 * `BriefDocument` — the one-page brief G7 approves (Stage 04 PRD §15.4 D).
 *
 * A document, not a panel: 72 characters to the line at 16 px, headed by the
 * server's word count against the 600 the schema allows. Every line ends in
 * the `SourceChip`s it stands on (law 1), proof points carry their claim and
 * its expiry, and the sections code copied from the pins say so rather than
 * pretending to a source chip they do not have.
 *
 * With `edits` set, the words of each line become editable in place; nothing
 * code wrote is offered. The edited words travel with G7's decision and the
 * server revalidates and re-hashes them.
 */
export function BriefDocument({
  view,
  projectId,
  edits,
  onEdit,
}: {
  view: CreativeBriefView;
  projectId: string;
  edits: BriefEdits | null;
  onEdit: (path: string, text: string) => void;
}) {
  const { brief } = view;
  const line = (value: BriefLine, path: string) => (
    <Line line={value} path={path} projectId={projectId} edits={edits} onEdit={onEdit} />
  );

  return (
    <article className="max-w-measure text-base leading-7 text-fg" aria-labelledby="brief-title">
      <header className="flex flex-col gap-2 border-b pb-5">
        <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-2">
          <h1 id="brief-title" className="text-xl font-semibold tracking-tight">
            Creative brief
          </h1>
          <WordCount count={view.word_count} limit={view.max_words} editing={edits !== null} />
        </div>
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-fg-muted">
          <span>Plan v{brief.plan_ref.version}</span>
          <span aria-hidden>·</span>
          <span>
            Ruleset <span className="font-mono text-xs">{brief.ruleset_ref.ruleset_version}</span>
          </span>
          <span aria-hidden>·</span>
          <span className="inline-flex items-center gap-1">
            Run <MonoId value={brief.creative_run_id} label="run id" />
          </span>
        </p>
      </header>

      <Section title="Objective">{line(brief.objective, "objective")}</Section>

      <Section title="Who it is for">
        <ul className="flex flex-col gap-3">
          {brief.audience.map((value, i) => (
            <li key={i}>{line(value, `audience.${i}`)}</li>
          ))}
        </ul>
      </Section>

      {brief.exclusions.length > 0 ? (
        <Section title="Who it is not for">
          <ul className="flex flex-col gap-3">
            {brief.exclusions.map((value, i) => (
              <li key={i}>{line(value, `exclusions.${i}`)}</li>
            ))}
          </ul>
        </Section>
      ) : null}

      <Section title="The angle">{line(brief.angle, "angle")}</Section>

      <Section title="Proof points">
        {brief.proof_points.length === 0 ? (
          <p className="text-fg-muted">
            No licensed claim at this ruleset pin, so the copy asserts none. A claim is licensed
            in the claims register, and the next brief can carry it.
          </p>
        ) : (
          <ul className="flex flex-col gap-3">
            {brief.proof_points.map((claim) => (
              <li key={claim.claim_id} className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span>{claim.normalized_text}</span>
                <span className="inline-flex flex-wrap items-center gap-2 text-sm text-fg-muted">
                  <Badge>
                    Claim <MonoId value={claim.claim_id} label="claim id" /> · {CLAIM_STATUS[claim.status]}
                  </Badge>
                  <span className="tabular-nums">
                    {claim.expires_at ? `expires ${day(claim.expires_at)}` : "does not expire"}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="Offer">
        {brief.offer ? (
          <Offer offer={brief.offer} />
        ) : (
          <p className="text-fg-muted">No promotion or price offer runs with this brief.</p>
        )}
      </Section>

      <Section title="Ad groups">
        <div className="flex flex-col gap-6">
          {brief.ad_groups.map((group, i) => (
            <AdGroup
              key={`${group.campaign_ref}/${group.ad_group_ref}`}
              group={group}
              index={i}
              line={line}
              edits={edits}
              onEdit={onEdit}
            />
          ))}
        </div>
      </Section>

      <Section title="Non-negotiables" note="Copied in code from the pinned ruleset; not model-written.">
        <dl className="flex flex-col gap-2">
          <Terms label="Sounds like" terms={brief.non_negotiables.voice_words} />
          <Terms label="Never says" terms={brief.non_negotiables.never_terms} />
          <Terms label="Always says" terms={brief.non_negotiables.required_terms} />
          <Terms label="Discloses" terms={brief.non_negotiables.disclosures} />
        </dl>
      </Section>

      <Section title="Visual constraints" note="Copied in code from the pinned ruleset; not model-written.">
        <dl className="flex flex-col gap-2">
          <Terms label="May show" terms={brief.visual_constraints.permitted_subjects} />
          <Terms label="Never shows" terms={brief.visual_constraints.forbidden_subjects} />
          <Terms label="Palette" terms={brief.visual_constraints.palette_tokens} />
          <Fact label="The product">{DEPICTION[brief.visual_constraints.product_depiction]}</Fact>
        </dl>
      </Section>

      <Section title="Media plan">
        <MediaPlan view={view} projectId={projectId} />
      </Section>
    </article>
  );
}

function Section({ title, note, children }: { title: string; note?: string; children: ReactNode }) {
  return (
    <section className="border-b py-6 last:border-b-0">
      <h2 className="text-lg font-semibold tracking-tight">{title}</h2>
      {note ? <p className="mt-0.5 text-sm text-fg-subtle">{note}</p> : null}
      <div className="mt-3">{children}</div>
    </section>
  );
}

/**
 * `512/600` — the server's count over the rendered brief (§15.5 item 1: the
 * count is display; the verdict is the schema's). While editing it is the
 * count on record, and says it is recounted on approval.
 */
function WordCount({ count, limit, editing }: { count: number; limit: number; editing: boolean }) {
  const near = count >= limit * NEAR_LIMIT;
  return (
    <p className="flex items-center gap-1.5 text-sm text-fg-muted">
      {near ? <TriangleAlert className="size-4 text-status-gate" aria-hidden /> : null}
      <span className={cn("tabular-nums", near ? "font-medium text-fg" : undefined)}>
        {count}/{limit}
      </span>
      <span>{editing ? "words, recounted when you approve" : "words"}</span>
    </p>
  );
}

function Line({
  line,
  path,
  projectId,
  edits,
  onEdit,
}: {
  line: BriefLine;
  path: string;
  projectId: string;
  edits: BriefEdits | null;
  onEdit: (path: string, text: string) => void;
}) {
  const chips = (
    <span className="inline-flex flex-wrap gap-1 align-baseline">
      {line.sources.map((source, i) => (
        <SourceChip key={i} source={source} projectId={projectId} />
      ))}
    </span>
  );
  if (edits !== null) {
    return (
      <div className="flex flex-col gap-1.5">
        <Textarea
          aria-label={`Edit ${path.replaceAll(".", " ")}`}
          value={edits[path] ?? line.text}
          onChange={(event) => onEdit(path, event.target.value)}
          className="field-sizing-content min-h-0 text-base leading-7"
        />
        {chips}
      </div>
    );
  }
  return (
    <p>
      {line.text} {chips}
    </p>
  );
}

function AdGroup({
  group,
  index,
  line,
  edits,
  onEdit,
}: {
  group: AdGroupBrief;
  index: number;
  line: (value: BriefLine, path: string) => ReactNode;
  edits: BriefEdits | null;
  onEdit: (path: string, text: string) => void;
}) {
  const themePath = `ad_groups.${index}.theme`;
  return (
    <section aria-label={`Ad group ${group.ad_group_ref}`} className="flex flex-col gap-3">
      <header>
        <h3 className="font-medium">{group.ad_group_ref}</h3>
        <p className="text-sm text-fg-muted">
          {group.campaign_ref} · measured by {group.kpi}
        </p>
      </header>
      <dl className="flex flex-col gap-3">
        <div>
          <dt className="text-sm text-fg-muted">Theme</dt>
          <dd>
            {edits !== null ? (
              <Textarea
                aria-label={`Edit the theme of ${group.ad_group_ref}`}
                value={edits[themePath] ?? group.theme}
                onChange={(event) => onEdit(themePath, event.target.value)}
                className="field-sizing-content min-h-0 text-base leading-7"
              />
            ) : (
              group.theme
            )}
          </dd>
        </div>
        <div>
          <dt className="text-sm text-fg-muted">Says first</dt>
          <dd>{line(group.primary_message, `ad_groups.${index}.primary_message`)}</dd>
        </div>
        <div>
          <dt className="text-sm text-fg-muted">Variant B says</dt>
          <dd>{line(group.angle_b, `ad_groups.${index}.angle_b`)}</dd>
        </div>
        {group.top_keywords.length > 0 ? (
          <div>
            <dt className="text-sm text-fg-muted">Top keywords</dt>
            <dd className="font-mono text-sm">{group.top_keywords.join(" · ")}</dd>
          </div>
        ) : null}
        <div>
          <dt className="text-sm text-fg-muted">Lands on</dt>
          <dd>
            <a
              href={group.landing_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 break-all text-accent underline-offset-4 hover:underline"
            >
              {group.landing_url}
              <ExternalLink className="size-3.5 shrink-0" aria-hidden />
            </a>
          </dd>
        </div>
      </dl>
    </section>
  );
}

/** One labelled row of a fact list: label above on a phone, beside it from `sm`. */
function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5 sm:flex-row sm:gap-6">
      <dt className="shrink-0 text-sm text-fg-muted sm:w-32 sm:pt-0.5">{label}</dt>
      <dd className="min-w-0">{children}</dd>
    </div>
  );
}

function Terms({ label, terms }: { label: string; terms: string[] }) {
  return (
    <Fact label={label}>
      {terms.length > 0 ? terms.join(", ") : <span className="text-fg-subtle">None set</span>}
    </Fact>
  );
}

/**
 * An offer as its binding carries it. Every number and date in it is an
 * `OfferRecord` field (law 35), shown resolved exactly as the binding holds
 * it — never reformatted into something the record does not say.
 */
function Offer({ offer }: { offer: OfferBinding }) {
  const resolved = Object.entries(offer.resolved);
  return (
    <div className="flex flex-col gap-2">
      <p>
        <span className="font-medium">{offer.sku_or_set}</span>{" "}
        <span className="text-sm text-fg-muted">
          bound to offer <MonoId value={offer.offer_record_id} label="offer record id" />
        </span>
      </p>
      {resolved.length > 0 ? (
        <dl className="flex flex-col gap-1 text-sm">
          {resolved.map(([field, value]) => (
            <Fact key={field} label={field.replaceAll("_", " ")}>
              <span className="tabular-nums">{value}</span>
            </Fact>
          ))}
        </dl>
      ) : null}
    </div>
  );
}

function MediaPlan({ view, projectId }: { view: CreativeBriefView; projectId: string }) {
  const plan = view.brief.media_plan;
  const images = plan.jobs.image ?? 0;
  const videos = plan.jobs.video ?? 0;
  return (
    <div className="flex flex-col gap-3">
      <p>
        {plan.images || plan.video ? (
          <>
            {images} image {images === 1 ? "job" : "jobs"} and {videos} video{" "}
            {videos === 1 ? "job" : "jobs"}, estimated at{" "}
            <span className="font-medium tabular-nums">{usd(plan.media_usd)}</span> of media within{" "}
            <span className="tabular-nums">{usd(plan.total_usd)}</span> in all.
          </>
        ) : (
          <>
            Copy only: no image or video is generated. Estimated at{" "}
            <span className="font-medium tabular-nums">{usd(plan.total_usd)}</span> in all.
          </>
        )}{" "}
        <EvidenceIdsChip
          label="calc"
          title="Priced by calc/ (media.cost_estimate_v1, media.ratio_plan_v1)"
          ids={plan.calc_evidence_ids}
          projectId={projectId}
        />
      </p>
      <p className="text-sm text-fg-muted">
        Confidence {plan.confidence}.{" "}
        {plan.fits ? "Fits both caps." : "Does not fit the caps as scoped."}
      </p>
      {Object.keys(plan.ratios).length > 0 ? (
        <dl className="flex flex-col gap-1 text-sm">
          {Object.entries(plan.ratios).map(([modality, ratios]) => (
            <Fact key={modality} label={modality === "video" ? "Video ratios" : "Image ratios"}>
              <span className="tabular-nums">
                {Object.entries(ratios)
                  .map(([ratio, how]) => `${ratio} ${how}`)
                  .join(" · ")}
              </span>
            </Fact>
          ))}
        </dl>
      ) : null}
    </div>
  );
}
