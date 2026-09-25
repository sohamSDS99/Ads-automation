"use client";

import { ExternalLink, Lock } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { MonoId } from "@/components/creative/mono-id";
import type { OfferBinding, OfferSource } from "@/lib/api/creative-runs";
import { absoluteTime } from "@/lib/format";
import { boundFigure, formatDate, offerEnd, remaining } from "@/lib/creative/offer-window";

/**
 * `OfferBindingField` — a promotion's or price's figures as the offer data
 * holds them (Stage 04 PRD §15.4 F, law 35): `20% off · ends 12 Oct 2026 ·
 * 18 days`.
 *
 * **Never editable, by construction.** There is no input here to disable: the
 * figures are an `OfferRecord`'s, rendered by the server from the run's pinned
 * snapshot, and the only way to change one is to change the offer data and run
 * again. What the field offers instead is the way back to that data — the
 * record's evidence row — and the window the asset was written against.
 */
export function OfferBindingField({
  binding,
  offer,
  projectId,
}: {
  binding: OfferBinding;
  offer: OfferSource | null;
  projectId: string;
}) {
  const figure = boundFigure(binding);
  const end = offerEnd(offer, binding);
  // Days are counted from the reader's today, so they are drawn after mount:
  // a server-rendered count would be the server's today.
  const [now, setNow] = useState<Date | null>(null);
  useEffect(() => setNow(new Date()), []);
  const bound = Object.entries(binding.resolved);

  return (
    <div
      role="group"
      aria-label={`Offer ${binding.sku_or_set}, read-only: bound to the offer record`}
      data-testid="offer-binding-field"
      className="flex min-w-0 flex-col gap-1.5 rounded-token border border-dashed bg-surface px-3 py-2"
    >
      <p className="flex flex-wrap items-baseline gap-x-1.5 text-sm text-fg">
        <Lock className="size-3.5 shrink-0 self-center text-fg-muted" aria-hidden />
        {figure ? <span className="font-medium tabular-nums">{figure}</span> : null}
        {end ? (
          <>
            {figure ? <span aria-hidden className="text-fg-subtle">·</span> : null}
            <span>
              ends{" "}
              <time dateTime={end} title={absoluteTime(end)} className="tabular-nums">
                {formatDate(end)}
              </time>
            </span>
            {now ? (
              <>
                <span aria-hidden className="text-fg-subtle">·</span>
                <span className="tabular-nums" data-testid="offer-remaining">
                  {remaining(end, now)}
                </span>
              </>
            ) : null}
          </>
        ) : (
          <span className="text-fg-muted">{figure ? "· " : ""}no end date on the offer record</span>
        )}
      </p>

      <dl className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-fg-muted">
        {bound.map(([field, value]) => (
          <div key={field} className="flex items-baseline gap-1">
            <dt>{field.replaceAll("_", " ")}</dt>
            <dd className="font-mono text-fg tabular-nums" title={`Bound to the record's ${binding.fields[field] ?? field}`}>
              {field === "start" || field === "end" ? formatDate(value) : value}
            </dd>
          </div>
        ))}
      </dl>

      <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-fg-muted">
        <span>
          {offer ? (
            <>
              {offer.sku} · {offer.product_set} · {offer.market}
            </>
          ) : (
            binding.sku_or_set
          )}
        </span>
        {offer?.evidence_id ? (
          <Link
            href={`/projects/${projectId}/evidence?ids=${offer.evidence_id}`}
            className="inline-flex items-center gap-1 text-accent hover:underline"
          >
            Open offer record
            <ExternalLink className="size-3" aria-hidden />
          </Link>
        ) : (
          <span>
            {offer
              ? "The offer record row this was rendered from is no longer stored."
              : "The record this was rendered from is not in the run's offer snapshot."}{" "}
            Offer <MonoId value={binding.offer_record_id} label="offer record id" />
          </span>
        )}
      </p>
    </div>
  );
}
