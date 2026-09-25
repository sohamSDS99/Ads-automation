"use client";

import { Monitor, Scissors, Smartphone } from "lucide-react";
import { Fragment, useLayoutEffect, useRef, useState } from "react";

import { SegmentedControl } from "@/components/ui/segmented";
import { measuredText } from "@/lib/creative/char-count";
import { cn } from "@/lib/utils";

export type Device = "desktop" | "mobile";

export type SerpLine = { id: string; label: string; text: string; surface: string };

type Fit = "shown" | "cut" | "hidden";

/** A headline joins the next one with Google's separator. */
const SEPARATOR = " | ";

/**
 * `SerpPreview` — the selected combination as Google's results page draws it,
 * at true width (Stage 04 PRD §15.4 E, §15.2 rule 1): Google's column width,
 * type sizes and face (`--font-serp`), the headlines on one line on desktop
 * and two on a phone, the descriptions on two and three.
 *
 * **Truncation is measured, not guessed.** After layout every headline and
 * description is located against the frame: shown whole, cut, or not shown at
 * all. What was cut is marked in the gutter beside the line and named below
 * the frame in words, so it is never colour alone (rule 14). The final lint and
 * rendered previews (4.6.4) are the record; this is the live view while editing.
 */
export function SerpPreview({
  device,
  onDevice,
  headlines,
  descriptions,
  finalUrl,
  paths,
  caption,
}: {
  device: Device;
  onDevice: (device: Device) => void;
  headlines: SerpLine[];
  descriptions: SerpLine[];
  finalUrl: string | null;
  paths: [string | null, string | null];
  /** What is being shown, in words: "Combination 1" or "H3 with H7 from the pair grid". */
  caption: string;
}) {
  const titleRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const [fits, setFits] = useState<Record<string, Fit>>({});
  const shown = { headlines: headlines.map(display), descriptions: descriptions.map(display) };
  const signature = JSON.stringify([device, shown]);

  useLayoutEffect(() => {
    const measure = () => {
      const next: Record<string, Fit> = {};
      for (const frame of [titleRef.current, bodyRef.current]) {
        if (!frame) continue;
        const box = frame.getBoundingClientRect();
        for (const span of frame.querySelectorAll<HTMLElement>("[data-line]")) {
          next[span.dataset.line as string] = fitOf(span, box);
        }
      }
      setFits(next);
    };
    measure();
    // A system face needs no loading, but a fallback face may swap in late.
    void document.fonts?.ready.then(measure);
  }, [signature]);

  const cut = [...headlines, ...descriptions].filter((line) => (fits[line.id] ?? "shown") !== "shown");
  const host = hostOf(finalUrl);
  const path = paths.filter((part): part is string => Boolean(part)).join("/");
  const mobile = device === "mobile";

  return (
    <section aria-labelledby="serp-preview-title" className="flex min-w-0 flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h2 id="serp-preview-title" className="text-sm font-medium text-fg">
            Search result preview
          </h2>
          <p className="text-xs text-fg-muted">{caption}</p>
        </div>
        <SegmentedControl<Device>
          label="Preview device"
          value={device}
          onChange={onDevice}
          options={[
            { value: "desktop", label: "Desktop · 600 px" },
            { value: "mobile", label: "Mobile · 328 px" },
          ]}
        />
      </div>

      {/* The frame keeps its true width; a narrow screen scrolls it rather
          than squeezing it, because a squeezed frame measures nothing. */}
      <div
        role="region"
        aria-label={`Search result at ${mobile ? "328" : "600"} px`}
        tabIndex={0}
        className="relative overflow-x-auto rounded-token border bg-surface p-4 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
      >
        <div className="flex w-max items-start gap-2">
          <div
            data-testid="serp-frame"
            data-device={device}
            data-truncated={cut.length > 0 ? "true" : "false"}
            className={cn(
              "shrink-0 bg-serp-bg px-0 py-3 font-serp",
              mobile ? "w-serp-mobile" : "w-serp-desktop",
            )}
          >
            <p className="text-serp-sm font-bold text-serp-url">Sponsored</p>
            <p className="truncate text-serp-sm text-serp-url">
              {host}
              {path ? <span className="text-serp-text">/{path}</span> : null}
            </p>
            <div
              ref={titleRef}
              className={cn(
                "mt-1 text-serp-title",
                mobile ? "line-clamp-2 text-serp-md" : "overflow-hidden text-ellipsis whitespace-nowrap text-serp-lg",
              )}
            >
              {headlines.map((line, index) => (
                <Fragment key={line.id}>
                  {index > 0 ? SEPARATOR : null}
                  <span data-line={line.id} data-fit={fits[line.id] ?? "shown"}>
                    {display(line)}
                  </span>
                </Fragment>
              ))}
            </div>
            <div
              ref={bodyRef}
              className={cn("mt-1 text-serp-sm text-serp-text", mobile ? "line-clamp-3" : "line-clamp-2")}
            >
              {descriptions.map((line, index) => (
                <Fragment key={line.id}>
                  {index > 0 ? " " : null}
                  <span data-line={line.id} data-fit={fits[line.id] ?? "shown"}>
                    {display(line)}
                  </span>
                </Fragment>
              ))}
            </div>
          </div>
          {/* The gutter: one mark per cut line, level with it. */}
          <div aria-hidden className="flex w-6 shrink-0 flex-col gap-1 pt-12 text-status-failed-ink">
            {cut.some((line) => headlines.includes(line)) ? <Scissors className="size-4" /> : <span className="size-4" />}
            {cut.some((line) => descriptions.includes(line)) ? <Scissors className="mt-2 size-4" /> : null}
          </div>
        </div>
      </div>

      <div aria-live="polite" className="text-xs">
        {cut.length === 0 ? (
          <p className="inline-flex items-center gap-1.5 text-fg-muted">
            {mobile ? <Smartphone className="size-3.5" aria-hidden /> : <Monitor className="size-3.5" aria-hidden />}
            Every headline and description fits at {mobile ? "328" : "600"} px.
          </p>
        ) : (
          <ul className="flex flex-col gap-1" data-testid="serp-truncation">
            {cut.map((line) => (
              <li key={line.id} className="inline-flex items-baseline gap-1.5 text-status-failed-ink">
                <Scissors className="size-3.5 shrink-0 self-center" aria-hidden />
                <span>
                  <span className="font-medium">{line.label}</span>{" "}
                  {fits[line.id] === "hidden"
                    ? `is not shown: no room left at ${mobile ? "328" : "600"} px.`
                    : `is cut at ${mobile ? "328" : "600"} px: “${display(line)}”.`}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

/** A keyword insertion shows its default text, as when no keyword fits. */
function display(line: SerpLine): string {
  return measuredText(line.surface, line.text);
}

/**
 * Where a line stands against its frame. The span's bounding box, not its
 * client rects: under `text-overflow: ellipsis` Chromium splits a cut span's
 * rects around the ellipsis, and the last of them sits inside the frame.
 */
function fitOf(span: HTMLElement, box: DOMRect): Fit {
  const rect = span.getBoundingClientRect();
  if (rect.width === 0 && rect.height === 0) return "hidden";
  const slack = 0.5;
  if (rect.left >= box.right - slack || rect.top >= box.bottom - slack) return "hidden";
  if (rect.right > box.right + slack || rect.bottom > box.bottom + slack) return "cut";
  return "shown";
}

function hostOf(url: string | null): string {
  if (!url) return "example.com";
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}
