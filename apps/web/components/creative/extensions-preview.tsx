import { Tag } from "lucide-react";

import type { Device } from "@/components/creative/serp-preview";
import { formatDate } from "@/lib/creative/offer-window";
import { cn } from "@/lib/utils";

export type PreviewSitelink = { id: string; text: string; line1: string; line2: string };
/** `ends`: the offer record's end, ISO 8601, as the binding renders it. */
export type PreviewPromotion = { id: string; figure: string; text: string; ends: string | null };
export type PreviewPrice = { id: string; header: string; figure: string };

/**
 * `ExtensionsPreview` — a campaign's extras drawn under its ad, inside the
 * same true-width result frame (Stage 04 PRD §15.4 F, §15.2 rule 1): sitelinks
 * as Google lays them out on each device, then callouts, the structured
 * snippet, the promotion and the price items, in Google's type and colours.
 *
 * Which extensions Google shows beside an ad is decided per auction; this
 * draws every one the run wrote, in the order it wrote them, and says so in
 * the frame's caption rather than implying a guaranteed layout.
 */
export function ExtensionsPreview({
  device,
  sitelinks,
  callouts,
  snippet,
  promotion,
  prices,
}: {
  device: Device;
  sitelinks: PreviewSitelink[];
  callouts: string[];
  snippet: { header: string; values: string[] } | null;
  promotion: PreviewPromotion | null;
  prices: PreviewPrice[];
}) {
  const mobile = device === "mobile";
  const nothing = sitelinks.length + callouts.length + prices.length === 0 && !snippet && !promotion;
  if (nothing) return null;
  return (
    <div data-testid="extensions-preview" className="mt-2 flex flex-col gap-2 text-serp-sm text-serp-text">
      {callouts.length > 0 ? <p data-extension="callouts">{callouts.join(" · ")}</p> : null}
      {snippet ? (
        <p data-extension="snippet">
          {snippet.header}: {snippet.values.join(", ")}
        </p>
      ) : null}
      {promotion ? (
        <p data-extension="promotion" className="flex items-baseline gap-1.5">
          <Tag className="size-3.5 shrink-0 self-center" aria-hidden />
          <span>
            <span className="text-serp-title">
              {promotion.figure} {promotion.text}
            </span>
            {promotion.ends ? (
              <>
                {" "}
                · Until <time dateTime={promotion.ends}>{formatDate(promotion.ends)}</time>
              </>
            ) : null}
          </span>
        </p>
      ) : null}
      {sitelinks.length > 0 ? (
        <ul
          data-extension="sitelinks"
          className={cn(mobile ? "flex flex-col divide-y divide-serp-rule border-y border-serp-rule" : "grid grid-cols-2 gap-x-6 gap-y-2")}
        >
          {sitelinks.map((link) => (
            <li key={link.id} className={cn("min-w-0", mobile && "py-2")}>
              <p className="truncate text-serp-md text-serp-title">{link.text}</p>
              {mobile ? null : (
                <>
                  <p className="truncate">{link.line1}</p>
                  <p className="truncate">{link.line2}</p>
                </>
              )}
            </li>
          ))}
        </ul>
      ) : null}
      {prices.length > 0 ? (
        <ul data-extension="prices" className="flex gap-2 overflow-hidden">
          {prices.map((price) => (
            <li key={price.id} className="min-w-0 flex-1 rounded-token border border-serp-rule px-2 py-1.5">
              <p className="truncate text-serp-title">{price.header}</p>
              <p className="tabular-nums">{price.figure}</p>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
