"use client";

import { ImageOff, Quote } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { LintChip } from "@/components/creative/lint-chip";
import { LetterboxFrame } from "@/components/creative/media-frame";
import { Badge } from "@/components/ui/badge";
import type { ProductDepiction } from "@/lib/api/creative-runs";
import { mediaContentUrl, type ConceptMasters } from "@/lib/api/media-library";
import { angleSource, ratioValue, tokenColour, type BoardConcept } from "@/lib/creative/media-library";
import { cn } from "@/lib/utils";

const DEPICTION: Record<ProductDepiction, { label: string; means: string }> = {
  reference_guided: {
    label: "Reference-guided",
    means: "The model was shown the attested product references (law 44).",
  },
  composited_real: {
    label: "Real product composited",
    means: "The model paints the scene; the real product is composited in code (law 38).",
  },
  none: {
    label: "No product shown",
    means: "Neither a reference nor a composite is allowed here, so the product is not depicted.",
  },
};

const LINE_LABEL = { angle: "the brief’s angle", primary_message: "primary message", angle_b: "variant-B angle" };

/**
 * `ConceptBoard` (Stage 04 PRD §15.4 G): one column per 4.4.1 concept — its
 * name, the rationale beside the brief angle it is built on (linked to the
 * brief), the palette tokens it uses as swatches, the `product_depiction`
 * code resolved for it, and the master 4.4.2 chose, drawn from its WebP proxy
 * at its true ratio. The master file itself loads only in the detail drawer.
 */
export function ConceptBoard({
  concepts,
  masters,
  palette,
  briefHref,
  onOpenMaster,
}: {
  concepts: BoardConcept[];
  masters: ConceptMasters[];
  /** The brand's `{name, hex}` colour tokens at the guideline the run pinned. */
  palette: { name?: unknown; hex?: unknown }[];
  briefHref: string;
  onOpenMaster: (concept: BoardConcept, master: ConceptMasters) => void;
}) {
  return (
    <ol
      aria-label="Concepts"
      className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3"
      data-testid="concept-board"
    >
      {concepts.map((concept) => (
        <ConceptColumn
          key={concept.id}
          concept={concept}
          masters={masters.find((item) => item.concept_id === concept.id) ?? null}
          palette={palette}
          briefHref={briefHref}
          onOpenMaster={onOpenMaster}
        />
      ))}
    </ol>
  );
}

function ConceptColumn({
  concept,
  masters,
  palette,
  briefHref,
  onOpenMaster,
}: {
  concept: BoardConcept;
  masters: ConceptMasters | null;
  palette: { name?: unknown; hex?: unknown }[];
  briefHref: string;
  onOpenMaster: (concept: BoardConcept, master: ConceptMasters) => void;
}) {
  const source = angleSource(concept.angle);
  const depiction = DEPICTION[concept.product_depiction];
  return (
    <li className="flex min-w-0 flex-col gap-3 rounded-token border bg-surface p-3" data-concept={concept.id}>
      <header className="flex min-w-0 flex-col gap-1">
        <h3 className="text-md font-semibold tracking-tight text-fg">{concept.name}</h3>
        <p className="truncate font-mono text-xs text-fg-subtle" title={concept.id}>
          {concept.campaign_ref} · {concept.campaign_type}
        </p>
      </header>

      <Master concept={concept} masters={masters} onOpen={onOpenMaster} />

      <p className="text-sm text-fg">{concept.rationale}</p>

      <figure className="flex flex-col gap-1 border-l pl-3 text-sm">
        <figcaption className="flex items-center gap-1 text-xs text-fg-muted">
          <Quote className="size-3" aria-hidden />
          Built on {source.adGroupRef ? `${source.adGroupRef}’s ` : ""}
          {LINE_LABEL[source.line]}
        </figcaption>
        <blockquote className="text-fg-muted">
          <Link href={briefHref} className="underline decoration-border-strong underline-offset-2 hover:text-fg">
            {concept.angle_text}
          </Link>
        </blockquote>
      </figure>

      <div className="flex flex-col gap-1.5">
        <p className="text-xs font-medium text-fg-muted">Palette</p>
        {concept.palette_tokens.length === 0 ? (
          <p className="text-xs text-fg-muted">No brand colour was named for this concept.</p>
        ) : (
          <ul className="flex flex-wrap gap-2" aria-label="Palette tokens">
            {concept.palette_tokens.map((token) => {
              const colour = tokenColour(token, palette);
              return (
                <li key={token} className="inline-flex items-center gap-1.5 text-xs text-fg">
                  <span
                    aria-hidden
                    className={cn("size-4 rounded-sm border", colour ? "" : "border-dashed")}
                    style={colour ? { backgroundColor: colour } : undefined}
                  />
                  <span>{token}</span>
                  {colour && colour !== token ? (
                    <span className="font-mono text-fg-subtle">{colour.toLowerCase()}</span>
                  ) : null}
                  {colour ? null : <span className="text-fg-subtle">(colour not on record)</span>}
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <p className="flex flex-wrap items-center gap-2 text-xs text-fg-muted" title={depiction.means}>
        <Badge>{depiction.label}</Badge>
        <span className="min-w-0">{depiction.means}</span>
      </p>
    </li>
  );
}

function Master({
  concept,
  masters,
  onOpen,
}: {
  concept: BoardConcept;
  masters: ConceptMasters | null;
  onOpen: (concept: BoardConcept, master: ConceptMasters) => void;
}) {
  const [missing, setMissing] = useState(false);
  if (!masters) {
    return (
      <p className="rounded-token border border-dashed px-3 py-6 text-center text-xs text-fg-muted">
        4.4.2 has not made a master for this concept yet.
      </p>
    );
  }
  if (!masters.master) {
    return (
      <p className="rounded-token border border-dashed px-3 py-4 text-xs text-fg-muted">
        <span className="font-medium text-fg">No master.</span> {masters.gap?.detail ?? "4.4.2 recorded no reason."}
      </p>
    );
  }
  const winner = masters.candidates.find((item) => item.media_id === masters.master?.media_id);
  const ratio = ratioValue(masters.aspect_ratio ?? "") ?? 1;
  return (
    <div className="flex flex-col gap-1.5">
      <button
        type="button"
        onClick={() => onOpen(concept, masters)}
        aria-label={`Open the master of ${concept.name}`}
        className="rounded-token focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
      >
        <LetterboxFrame ratio={ratio} frameRatio={4 / 3}>
          {missing ? (
            <span className="grid size-full place-items-center bg-surface text-xs text-fg-muted">
              <ImageOff className="size-4" aria-hidden />
              No preview proxy
            </span>
          ) : (
            // eslint-disable-next-line @next/next/no-img-element -- a signed, redirected file
            <img
              src={mediaContentUrl(masters.master.media_id, "preview")}
              alt={`Master image for ${concept.name}`}
              loading="lazy"
              decoding="async"
              onError={() => setMissing(true)}
              className="block size-full object-contain"
            />
          )}
        </LetterboxFrame>
      </button>
      <p className="flex flex-wrap items-center gap-1.5 text-xs text-fg-muted">
        <span className="tabular-nums">{masters.aspect_ratio ?? "ratio not recorded"}</span>
        {winner ? <LintChip verdict={winner.lint.verdict} /> : null}
        <span className="min-w-0">{masters.master.why}</span>
      </p>
    </div>
  );
}
