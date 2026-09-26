"use client";

import {
  CircleCheck,
  CircleDashed,
  CircleX,
  Monitor,
  Smartphone,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";
import { useMemo, useState } from "react";

import { ScrollFrame } from "@/components/creative/scroll-frame";
import { Badge } from "@/components/ui/badge";
import { Select } from "@/components/ui/select";
import {
  previewScreenshotUrl,
  type PreviewDevice,
  type PreviewVerdict,
  type RenderPreviewItem,
} from "@/lib/api/creative-packages";
import {
  adRefParts,
  elementLabel,
  groupPreviews,
  markFor,
  rolesLabel,
  truncationText,
  truncations,
  type PreviewPair,
} from "@/lib/creative/package";
import { cn } from "@/lib/utils";

const VERDICT: Record<PreviewVerdict, { label: string; icon: LucideIcon; tone: "neutral" | "warning" | "danger"; ink: string }> = {
  pass: { label: "Fits", icon: CircleCheck, tone: "neutral", ink: "text-status-success" },
  warning: { label: "Warning", icon: TriangleAlert, tone: "warning", ink: "text-status-gate-ink" },
  blocking: { label: "Blocking", icon: CircleX, tone: "danger", ink: "text-status-failed-ink" },
  unavailable: { label: "Not drawn", icon: CircleDashed, tone: "neutral", ink: "text-fg-subtle" },
};

/** Room left of a cropped capture for the truncation numbers, in CSS px. */
const GUTTER = 20;

const DEVICE: Record<PreviewDevice, { label: string; icon: LucideIcon; placeholder: string }> = {
  mobile: { label: "Phone", icon: Smartphone, placeholder: "w-serp-mobile" },
  desktop: { label: "Desktop", icon: Monitor, placeholder: "w-serp-desktop" },
};

/** 4.6.4's verdict for one render, in words and an icon (§15.2 rule 14). */
export function PreviewVerdictChip({ verdict }: { verdict: PreviewVerdict }) {
  const { label, icon: Icon, tone, ink } = VERDICT[verdict];
  return (
    <Badge tone={tone} data-verdict={verdict} className="whitespace-nowrap">
      <Icon className={cn("size-3", ink)} aria-hidden />
      {label}
    </Badge>
  );
}

function isTruncated(item: RenderPreviewItem | null): boolean {
  return item !== null && truncations(item.dom_metrics).length > 0;
}

/**
 * `PreviewGrid` — every RSA combination 4.6.4 rendered, phone beside desktop,
 * **at true scale** (Stage 04 PRD §15.4 K): the capture is shown one image
 * pixel to one CSS pixel, cropped to the ad's own box, and a desktop ad that
 * is wider than the screen scrolls rather than shrinks.
 *
 * **Truncation is the render's, marked where it happened.** 4.6.4 measured
 * every element's box in the capture; each one it found overflowing its slot
 * or clipped by the layout is outlined on the picture and numbered, and the
 * same number names it below in words. The verdict chip is 4.6.4's — pixels
 * are advisory, and only the spec diff (listed under the render) blocks (D12).
 */
export function PreviewGrid({ previews }: { previews: RenderPreviewItem[] }) {
  const groups = useMemo(() => groupPreviews(previews), [previews]);
  const [adRef, setAdRef] = useState("");
  const [truncatedOnly, setTruncatedOnly] = useState(false);

  const shown = groups
    .filter((group) => !adRef || group.adRef === adRef)
    .map((group) => ({
      ...group,
      pairs: truncatedOnly ? group.pairs.filter((p) => isTruncated(p.mobile) || isTruncated(p.desktop)) : group.pairs,
    }))
    .filter((group) => group.pairs.length > 0);

  const truncated = previews.filter((p) => isTruncated(p)).length;
  const blocking = previews.filter((p) => p.verdict === "blocking").length;
  const undrawn = previews.filter((p) => !p.has_screenshot).length;
  const combinations = groups.reduce((sum, g) => sum + g.pairs.length, 0);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <p className="text-sm text-fg-muted tabular-nums" data-testid="preview-summary">
          {combinations} {combinations === 1 ? "combination" : "combinations"} of {groups.length}{" "}
          {groups.length === 1 ? "RSA" : "RSAs"} · {truncated} {truncated === 1 ? "render truncates" : "renders truncate"} ·{" "}
          {blocking} blocking{undrawn > 0 ? ` · ${undrawn} not drawn` : ""}
        </p>
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-sm text-fg">
            <input
              type="checkbox"
              checked={truncatedOnly}
              onChange={(event) => setTruncatedOnly(event.target.checked)}
              className="size-4 accent-accent"
            />
            Truncated only
          </label>
          <Select value={adRef} onChange={(event) => setAdRef(event.target.value)} aria-label="Show one RSA">
            <option value="">Every RSA</option>
            {groups.map((group) => (
              <option key={group.adRef} value={group.adRef}>
                {group.adRef}
              </option>
            ))}
          </Select>
        </div>
      </div>

      {shown.length === 0 ? (
        <p className="rounded-token border px-4 py-6 text-center text-sm text-fg-muted">
          No render truncates. Clear “Truncated only” to see every combination.
        </p>
      ) : (
        shown.map((group) => {
          const { campaign, adGroup, variant } = adRefParts(group.adRef);
          return (
            <section key={group.adRef} className="flex flex-col gap-4 border-t pt-4" data-testid="preview-rsa" data-ad-ref={group.adRef}>
              <h3 className="text-sm font-medium text-fg">
                {adGroup || group.adRef}
                {variant ? <span className="text-fg-muted"> · variant {variant}</span> : null}
                <span className="block text-xs font-normal text-fg-subtle">{campaign}</span>
              </h3>
              {group.pairs.map((pair) => (
                <Combination key={pair.key} pair={pair} adRef={group.adRef} />
              ))}
            </section>
          );
        })
      )}
    </div>
  );
}

function Combination({ pair, adRef }: { pair: PreviewPair; adRef: string }) {
  const name = rolesLabel(pair.roles);
  return (
    <figure className="flex min-w-0 flex-col gap-3" data-testid="preview-combination">
      <figcaption className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs text-fg-muted">
        <span className="font-medium text-fg">{name}</span>
        {pair.likelihood !== null ? (
          <span className="tabular-nums">likelihood {pair.likelihood.toFixed(3)}</span>
        ) : null}
      </figcaption>
      <div className="flex flex-wrap items-start gap-6">
        <Shot device="mobile" item={pair.mobile} caption={`${name} of ${adRef}`} />
        <Shot device="desktop" item={pair.desktop} caption={`${name} of ${adRef}`} />
      </div>
    </figure>
  );
}

function Shot({ device, item, caption }: { device: PreviewDevice; item: RenderPreviewItem | null; caption: string }) {
  const { label, icon: Icon, placeholder } = DEVICE[device];
  if (item === null) {
    return (
      <div className={cn("flex max-w-full flex-col gap-2", placeholder)}>
        <p className="flex items-center gap-1.5 text-xs text-fg-muted">
          <Icon className="size-3.5" aria-hidden />
          {label}
        </p>
        <p className="rounded-token border border-dashed px-4 py-6 text-sm text-fg-muted">
          4.6.4 recorded no {label.toLowerCase()} render of this combination.
        </p>
      </div>
    );
  }
  const dom = item.dom_metrics;
  const frame = dom.frame ?? null;
  const marks = truncations(dom);
  const spec = item.spec_diff;
  const drawn = item.has_screenshot;
  const alt =
    `${label} rendering of ${caption}` +
    (marks.length > 0 ? `, ${marks.length} ${marks.length === 1 ? "element" : "elements"} truncated` : ", nothing truncated");

  return (
    <div
      className="flex min-w-0 max-w-full flex-col gap-2"
      data-testid="preview-shot"
      data-device={device}
      data-verdict={item.verdict}
      data-truncated={marks.map((m) => m.key).join(" ")}
    >
      <p className="flex items-center gap-2 text-xs text-fg-muted">
        <Icon className="size-3.5" aria-hidden />
        {label}
        <PreviewVerdictChip verdict={item.verdict} />
      </p>
      {drawn ? (
        <ScrollFrame label={`${label} preview of ${caption}, at true scale`}>
          <div
            data-testid="preview-frame"
            className="relative overflow-clip rounded-token border bg-serp-bg"
            style={frame ? { width: Math.ceil(frame.width) + GUTTER, height: Math.ceil(frame.height) } : undefined}
          >
            {/* One capture pixel to one CSS pixel: `max-w-none` keeps the
                preflight's `max-width: 100%` from shrinking it. `overflow-clip`
                on the frame, not `hidden`: a hidden box can still be scrolled
                by script, focus or find-in-page, which would slide the capture
                out from under its marks. */}
            {/* eslint-disable-next-line @next/next/no-img-element -- a stored capture streamed by api, not a Next asset */}
            <img
              src={previewScreenshotUrl(item.id)}
              alt={alt}
              loading="lazy"
              decoding="async"
              className={cn("block max-w-none", frame && "absolute")}
              style={frame ? { left: GUTTER - frame.x, top: -frame.y } : undefined}
              data-testid="preview-image"
            />
            {marks.map((mark, index) => {
              const at = markFor(mark, frame);
              if (!at) return null;
              const { x, y, width, height } = at.rect;
              // Cropped frames keep a gutter left of the capture for the
              // numbers, so a mark at the ad's left edge keeps its whole badge.
              const shift = frame ? GUTTER : 0;
              return (
                <span key={mark.key} aria-hidden>
                  <span
                    data-testid="truncation-mark"
                    data-element={mark.key}
                    data-kind={at.kind}
                    className={cn(
                      "pointer-events-none absolute",
                      // A box where the text still shows; a cut line where a
                      // block's clamp or edge hid it.
                      at.kind === "box"
                        ? "rounded-sm outline-2 outline-offset-1 outline-status-failed-ink"
                        : "border-t-2 border-dashed border-status-failed-ink",
                    )}
                    style={{ left: x + shift, top: y, width, height }}
                  />
                  <span
                    className="pointer-events-none absolute flex size-4 items-center justify-center rounded-full bg-status-failed-ink text-xs font-medium text-bg tabular-nums"
                    style={{ left: frame ? 2 : Math.max(0, x - 8), top: Math.max(0, y + Math.min(height, 20) / 2 - 8) }}
                  >
                    {index + 1}
                  </span>
                </span>
              );
            })}
          </div>
        </ScrollFrame>
      ) : (
        <p className={cn("max-w-full rounded-token border border-dashed px-4 py-6 text-sm text-fg-muted", placeholder)}>
          Not drawn{dom.error ? `: ${dom.error}` : ""}. Its spec diff below still stands.
        </p>
      )}
      <Findings marks={marks} spec={spec} />
    </div>
  );
}

function Findings({ marks, spec }: { marks: ReturnType<typeof truncations>; spec: RenderPreviewItem["spec_diff"] }) {
  const mismatched = spec.mismatched ?? [];
  const counts = [...(spec.missing ?? []), ...(spec.extra ?? [])];
  const unchecked = spec.unchecked ?? [];
  if (marks.length === 0 && mismatched.length === 0 && counts.length === 0 && unchecked.length === 0) {
    return <p className="text-xs text-fg-subtle">Nothing truncated; the spec diff is clean.</p>;
  }
  return (
    <ul className="flex max-w-serp-desktop flex-col gap-1 text-xs text-fg">
      {marks.map((mark, index) => (
        <li key={`t-${mark.key}`} className="flex items-baseline gap-2" data-testid="truncation">
          <span className="flex size-4 shrink-0 items-center justify-center rounded-full bg-status-failed-ink text-xs font-medium text-bg tabular-nums">
            {index + 1}
          </span>
          <span>
            <span className="font-medium">{mark.label}</span> — {truncationText(mark)}
            {mark.box ? null : <span className="text-fg-muted"> (measured before boxes were recorded)</span>}
          </span>
        </li>
      ))}
      {mismatched.map((item) => (
        <li key={`m-${item.element}`} className="flex items-baseline gap-2" data-testid="spec-mismatch">
          <CircleX className="size-3.5 shrink-0 translate-y-0.5 text-status-failed-ink" aria-hidden />
          <span className="tabular-nums">
            <span className="font-medium">{elementLabel(item.element)}</span> is {item.measured} characters; the spec
            allows {item.expected}. Blocking.
          </span>
        </li>
      ))}
      {counts.map((item) => (
        <li key={`c-${item.asset_type}-${item.constraint}`} className="flex items-baseline gap-2">
          <CircleX className="size-3.5 shrink-0 translate-y-0.5 text-status-failed-ink" aria-hidden />
          <span className="tabular-nums">
            {item.measured} {item.asset_type.replace(/_/g, " ")}
            {item.measured === 1 ? "" : "s"}; the spec {item.constraint === "min_count" ? "needs at least" : "allows at most"}{" "}
            {item.expected}. Blocking.
          </span>
        </li>
      ))}
      {unchecked.map((item) => (
        <li key={`u-${item.element}`} className="flex items-baseline gap-2 text-fg-muted">
          <CircleDashed className="size-3.5 shrink-0 translate-y-0.5" aria-hidden />
          <span>
            {elementLabel(item.element)} has no spec for {item.surface.replace(/_/g, " ")} at the pin, so nothing
            checked it.
          </span>
        </li>
      ))}
    </ul>
  );
}
