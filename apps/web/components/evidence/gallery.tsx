"use client";

import { ExternalLink } from "lucide-react";

import { screenshotUrl, type EvidenceItem } from "@/lib/api/evidence";
import { absoluteTime, relativeTime } from "@/lib/format";

/**
 * Competitor creatives, as pictures (PRD §13.4 D).
 *
 * The rows are the same evidence the table shows; this is the reading of them
 * that matters for ad copy, where the layout of a competitor's grid *is* the
 * finding. Images stream from the worker's Volume through a signed, session-
 * checked route — there is no public URL for any of them.
 */
export function ScreenshotGallery({ items }: { items: EvidenceItem[] }) {
  if (items.length === 0) return null;
  return (
    <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      {items.map((item) => (
        <li key={item.id}>
          <figure className="overflow-hidden rounded-[var(--radius)] border bg-surface-raised">
            <a href={screenshotUrl(item.id)} target="_blank" rel="noreferrer" className="block">
              {/* Not `next/image` — the optimizer has no session, so it would
                  fetch a 401 instead of a picture. */}
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={screenshotUrl(item.id)}
                alt={advertiserOf(item) ?? "Competitor creative"}
                loading="lazy"
                className="h-48 w-full bg-surface object-cover object-top"
              />
            </a>
            <figcaption className="space-y-1 px-3 py-2.5">
              <p className="truncate text-sm font-medium text-fg">
                {advertiserOf(item) ?? item.kind}
              </p>
              <p className="line-clamp-2 text-xs text-fg-muted">{headlineOf(item)}</p>
              <p className="flex items-center justify-between gap-2 pt-0.5">
                <time
                  className="text-xs text-fg-subtle"
                  dateTime={item.fetched_at}
                  title={absoluteTime(item.fetched_at)}
                >
                  {relativeTime(item.fetched_at)}
                </time>
                {item.source_url ? (
                  <a
                    href={item.source_url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 text-xs text-accent hover:underline"
                  >
                    Source
                    <ExternalLink className="size-3" aria-hidden />
                  </a>
                ) : null}
              </p>
            </figcaption>
          </figure>
        </li>
      ))}
    </ul>
  );
}

function advertiserOf(item: EvidenceItem): string | null {
  const value = item.payload.advertiser;
  return typeof value === "string" && value ? value : null;
}

function headlineOf(item: EvidenceItem): string {
  for (const key of ["headline", "description", "offer"]) {
    const value = item.payload[key];
    if (typeof value === "string" && value) return value;
  }
  return item.kind;
}
