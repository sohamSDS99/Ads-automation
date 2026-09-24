import { Info, TriangleAlert } from "lucide-react";

import { Skeleton } from "@/components/ui/skeleton";
import type { CreativeEstimate } from "@/lib/api/creative";
import { usd } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * The three series, in fixed order: text, image, video (dataviz: colour
 * follows the entity, never its rank — a scope with video off keeps text
 * blue and images orange).
 */
const SERIES = [
  { key: "text", label: "Text", swatch: "bg-series-1" },
  { key: "image", label: "Images", swatch: "bg-series-2" },
  { key: "video", label: "Video", swatch: "bg-series-3" },
] as const;

const CONFIDENCE: Record<CreativeEstimate["confidence"], string> = {
  high: "Every figure is a catalogue price for these exact parameters.",
  medium: "Some media is priced from the nearest tier the catalogue lists, not an exact one.",
  low: "Includes assumed figures: the text cost at default routing, and any media the catalogue prices per token or without an exact tier.",
};

/** Where a dollar amount sits on the bar, as a share of the scale. */
function at(value: number, domain: number): string {
  return `${Math.min(100, Math.max(0, (value / domain) * 100))}%`;
}

/**
 * One cap on the bar: a 2px rule across it, labelled on the side the other
 * cap is not, so the two labels can never collide. A label near the right
 * edge hangs leftwards from its rule instead of running off the chart.
 */
function CapMarker({
  left,
  label,
  side,
  flip,
}: {
  left: string;
  label: string;
  side: "above" | "below";
  flip: boolean;
}) {
  return (
    <div className="pointer-events-none absolute inset-y-0" style={{ left }} aria-hidden>
      <div className="absolute -inset-y-1.5 -ml-px w-0.5 bg-fg" />
      <span
        className={cn(
          "absolute whitespace-nowrap text-xs tabular-nums text-fg-muted",
          side === "above" ? "bottom-full mb-2" : "top-full mt-2",
          flip ? "right-0 pr-1.5" : "left-0 pl-1.5",
        )}
      >
        {label}
      </span>
    </div>
  );
}

/**
 * `CostEstimate` — what this run is expected to spend, against both caps
 * (PRD §9.3, §15.4 B).
 *
 * A single stacked bar: text, images and video, in dollars, on one scale that
 * also holds both caps. The total cap is drawn where the total may reach;
 * the media cap is drawn where the media may reach *after* the text before
 * it (`text + media cap`), because that is where the orange-and-aqua part of
 * the bar would cross it. Beneath the bar, a table of the same numbers is the
 * legend, the value labels and the accessible view at once — slot 3 is under
 * 3:1 on white, so the values are never left to colour.
 *
 * Every number is the server's (`POST /creative/estimate`); `fits` is its
 * verdict and nothing here re-decides it. The arithmetic below — the media
 * subtotal and each cap's headroom — is only said, never acted on.
 */
export function CostEstimate({
  estimate,
  updating,
}: {
  estimate?: CreativeEstimate;
  /** A newer request is being priced; the numbers shown are the last answer. */
  updating: boolean;
}) {
  if (!estimate) {
    return (
      <div className="flex flex-col gap-3" aria-busy>
        <Skeleton className="h-6 w-40" />
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    );
  }

  const values = { text: estimate.text_usd, image: estimate.image_usd, video: estimate.video_usd };
  const media = estimate.image_usd + estimate.video_usd;
  const capTotal = estimate.caps.max_creative_cost_usd;
  const capMedia = estimate.caps.max_media_cost_usd;
  const mediaCapAt = estimate.text_usd + capMedia;
  const domain = Math.max(estimate.total_usd, capTotal, mediaCapAt) || 1;
  const drawn = SERIES.filter((series) => values[series.key] > 0);
  const mediaLeft = capMedia - media;
  const totalLeft = capTotal - estimate.total_usd;

  return (
    <figure className={cn("flex flex-col gap-4", updating && "opacity-60")} aria-busy={updating}>
      <figcaption className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <span className="text-sm text-fg-muted">
          Estimated{" "}
          <span className="text-lg font-semibold tabular-nums text-fg">{usd(estimate.total_usd)}</span>
        </span>
        <span className="inline-flex items-center gap-1.5 text-xs text-fg-muted" title={CONFIDENCE[estimate.confidence]}>
          <Info className="size-3.5" aria-hidden />
          Confidence {estimate.confidence}
          <span className="sr-only">. {CONFIDENCE[estimate.confidence]}</span>
        </span>
      </figcaption>

      {/* The bar, on one dollar scale with both caps. Decorative to assistive
          tech: the table below carries every number it shows. */}
      <div className="px-1 py-7" aria-hidden>
        <div className="relative h-3">
          <div className="absolute inset-0 rounded-r bg-surface-hover" />
          <div className="absolute inset-y-0 left-0 flex gap-0.5" style={{ width: at(estimate.total_usd, domain) }}>
            {drawn.map((series, index) => (
              <div
                key={series.key}
                title={`${series.label} · ${usd(values[series.key])}`}
                className={cn("h-full", series.swatch, index === drawn.length - 1 && "rounded-r")}
                style={{ flexGrow: values[series.key], flexBasis: 0 }}
              />
            ))}
          </div>
          <CapMarker
            left={at(capTotal, domain)}
            label={`Total cap ${usd(capTotal)}`}
            side="above"
            flip={capTotal / domain > 0.6}
          />
          <CapMarker
            left={at(mediaCapAt, domain)}
            label={`Media cap ${usd(capMedia)}`}
            side="below"
            flip={mediaCapAt / domain > 0.6}
          />
        </div>
      </div>

      <table className="w-full border-collapse text-sm">
        <caption className="sr-only">Estimated spend by kind, against both caps</caption>
        <thead>
          <tr className="text-left text-xs text-fg-subtle">
            <th scope="col" className="pb-1.5 font-medium">
              Spend
            </th>
            <th scope="col" className="pb-1.5 text-right font-medium">
              Jobs
            </th>
            <th scope="col" className="pb-1.5 text-right font-medium">
              Estimate
            </th>
            <th scope="col" className="pb-1.5 pl-3 text-right font-medium">
              Against cap
            </th>
          </tr>
        </thead>
        <tbody className="tabular-nums">
          {SERIES.map((series) => (
            <tr key={series.key} className="border-t">
              <th scope="row" className="py-1.5 text-left font-normal text-fg">
                <span className="inline-flex items-center gap-2">
                  <span className={cn("size-2.5 rounded-sm", series.swatch)} aria-hidden />
                  {series.label}
                </span>
              </th>
              <td className="py-1.5 text-right text-fg-muted">
                {series.key === "text" ? "—" : estimate.jobs[series.key]}
              </td>
              <td className="py-1.5 text-right text-fg">{usd(values[series.key])}</td>
              <td className="py-1.5 pl-3" />
            </tr>
          ))}
          <tr className="border-t">
            <th scope="row" className="py-1.5 text-left font-normal text-fg-muted">
              Media
            </th>
            <td className="py-1.5 text-right text-fg-muted">{estimate.jobs.image + estimate.jobs.video}</td>
            <td className="py-1.5 text-right text-fg">{usd(media)}</td>
            <td className="py-1.5 pl-3 text-right text-fg-muted">
              <Headroom left={mediaLeft} cap={capMedia} />
            </td>
          </tr>
          <tr className="border-t">
            <th scope="row" className="py-1.5 text-left font-medium text-fg">
              Total
            </th>
            <td className="py-1.5 text-right text-fg-muted" />
            <td className="py-1.5 text-right font-medium text-fg">{usd(estimate.total_usd)}</td>
            <td className="py-1.5 pl-3 text-right text-fg-muted">
              <Headroom left={totalLeft} cap={capTotal} />
            </td>
          </tr>
        </tbody>
      </table>
    </figure>
  );
}

/** `$39.40 of $40.00 left`, or `$3.10 over $40.00` with an icon — never colour alone. */
function Headroom({ left, cap }: { left: number; cap: number }) {
  if (left >= 0) {
    return (
      <span className="whitespace-nowrap">
        {usd(left)} of {usd(cap)} left
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 whitespace-nowrap font-medium text-fg">
      <TriangleAlert className="size-3.5 text-status-failed" aria-hidden />
      {usd(-left)} over {usd(cap)}
    </span>
  );
}
