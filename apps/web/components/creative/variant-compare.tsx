import { FlaskConical } from "lucide-react";

import type { AdGroupBrief } from "@/lib/api/creative-runs";
import { combination, type StudioAd } from "@/lib/creative/ad-studio";
import { measuredText } from "@/lib/creative/char-count";

/**
 * `VariantCompare` — the ad group's A and B side by side (Stage 04 PRD §15.4 E):
 * each led by its angle, showing the combination it serves first, and between
 * them what makes B a test rather than a paraphrase — `copy.distinctness_v1`
 * from A against `copy.variant_min_distance`, the hypothesis and the metric
 * it is judged on. The numbers and the comparison are 4.2.4's, as stored; a B
 * below the floor is never written, so there is nothing here to decide.
 */
export function VariantCompare({
  a,
  b,
  brief,
}: {
  a: StudioAd;
  b: StudioAd | null;
  brief: AdGroupBrief | null;
}) {
  const record = b?.variantB ?? null;
  return (
    <section aria-labelledby="variant-compare-title" className="flex min-w-0 flex-col gap-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 id="variant-compare-title" className="text-sm font-medium text-fg">
          Variant A and B
        </h2>
        {record ? (
          <p className="text-sm tabular-nums text-fg" data-testid="distinctness">
            <span className="text-fg-muted">distinctness</span> {record.distinctness_vs_a.toFixed(2)} ≥{" "}
            {record.variant_min_distance.toFixed(2)}
          </p>
        ) : null}
      </div>

      {b === null || record === null ? (
        <p className="rounded-token border border-dashed px-4 py-3 text-sm text-fg-muted">
          Variant B is written by node 4.2.4 from the brief’s angle for this ad group. It appears here once that node
          finishes.
        </p>
      ) : (
        <>
          <div className="flex flex-col gap-2 rounded-token border bg-surface-raised px-4 py-3">
            <p className="inline-flex items-start gap-2 text-sm text-fg">
              <FlaskConical className="mt-0.5 size-4 shrink-0 text-fg-muted" aria-hidden />
              <span>
                <span className="font-medium">Hypothesis.</span> {record.hypothesis}
              </span>
            </p>
            <p className="pl-6 text-xs text-fg-muted">
              Judged on <span className="text-fg">{record.primary_metric}</span> · distance measured by{" "}
              <span className="font-mono">{record.distinctness_metric}</span> from A, held to at least{" "}
              <span className="tabular-nums">{record.variant_min_distance.toFixed(2)}</span>
            </p>
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <Column ad={a} angle={brief?.primary_message.text ?? null} title="A" />
            <Column ad={b} angle={record.ad_b.angle} title="B" />
          </div>
        </>
      )}
    </section>
  );
}

function Column({ ad, angle, title }: { ad: StudioAd; angle: string | null; title: string }) {
  const shown = combination(ad);
  return (
    <div className="flex min-w-0 flex-col gap-3 rounded-token border bg-surface-raised p-4" data-testid={`variant-${title}`}>
      <div className="flex flex-col gap-1">
        <h3 className="text-sm font-medium text-fg">Variant {title}</h3>
        {angle ? <p className="text-sm text-fg-muted">{angle}</p> : null}
      </div>
      <dl className="flex flex-col gap-2 text-sm">
        <div>
          <dt className="text-xs text-fg-muted">Headlines served first</dt>
          <dd className="text-fg">
            {shown.headlines.map((asset) => measuredText(asset.surface, asset.text ?? "")).join(" | ")}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-fg-muted">Descriptions served first</dt>
          <dd className="text-fg">{shown.descriptions.map((asset) => asset.text).join(" ")}</dd>
        </div>
        <div className="flex gap-4 text-xs tabular-nums text-fg-muted">
          <span>{ad.headlines.length} headlines</span>
          <span>{ad.descriptions.length} descriptions</span>
          <span>{ad.headlineReserves.length + ad.descriptionReserves.length} in reserve</span>
        </div>
      </dl>
    </div>
  );
}
