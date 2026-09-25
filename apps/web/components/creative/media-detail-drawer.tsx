"use client";

import { useEffect, useRef, useState, type PointerEvent, type ReactNode, type SyntheticEvent } from "react";

import { GenerationPanel } from "@/components/creative/generation-panel";
import { LintChip } from "@/components/creative/lint-chip";
import { LetterboxFrame } from "@/components/creative/media-frame";
import { MonoId } from "@/components/creative/mono-id";
import { AiGeneratedChip } from "@/components/creative/rendition-grid";
import { Badge } from "@/components/ui/badge";
import { SegmentedControl } from "@/components/ui/segmented";
import { Sheet, SheetContent } from "@/components/ui/sheet";
import type { CreativeAssetItem, GenerationJobItem } from "@/lib/api/creative-runs";
import {
  mediaContentUrl,
  type CampaignVideo,
  type Candidate,
  type CandidateLint,
  type ConceptMasters,
  type ImageRenditionsOutput,
  type VideoRendition,
} from "@/lib/api/media-library";
import { usd } from "@/lib/format";
import {
  DERIVATION_LABEL,
  bytesOfLimit,
  pxLabel,
  ratioValue,
  secondsLabel,
  type BoardConcept,
  type FileTile,
} from "@/lib/creative/media-library";
import { cn } from "@/lib/utils";

export type DrawerSubject =
  | { kind: "file"; tile: FileTile }
  | { kind: "master"; concept: BoardConcept; masters: ConceptMasters }
  | { kind: "video"; video: CampaignVideo; rendition: VideoRendition };

const SOURCE_TYPE: Record<string, string> = {
  "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia": "Trained algorithmic media",
  "http://cv.iptc.org/newscodes/digitalsourcetype/compositeWithTrainedAlgorithmicMedia":
    "Composite with trained algorithmic media",
};

/**
 * `MediaDetailDrawer` (Stage 04 PRD §15.4 G): one file at full resolution —
 * the only place the Media Library loads a master (§15.5 items 2–3) — with
 * 100% zoom and pan, its provenance (model, provider, seed, job, cost), the
 * disclosure stamp as read back from the file when it was written, its lint
 * findings and its lineage from candidate to proxy; and, for
 * `creative_execute`, the `GenerationPanel`.
 */
export function MediaDetailDrawer({
  subject,
  onClose,
  runId,
  projectId,
  jobs,
  masters,
  renditions,
  assets,
  canRegenerate,
}: {
  subject: DrawerSubject | null;
  onClose: () => void;
  runId: string;
  projectId: string;
  jobs: GenerationJobItem[];
  masters: ConceptMasters[];
  renditions: ImageRenditionsOutput | null;
  assets: CreativeAssetItem[];
  canRegenerate: boolean;
}) {
  const view = subject ? describe(subject, masters) : null;
  // A master's pixel size is not in 4.4.2's output: read it off the file the
  // drawer loads, so "100%" is the file's own size and "Size" is measured.
  const [natural, setNatural] = useState<{ width: number; height: number } | null>(null);
  const viewKey = view?.mediaId ?? "";
  useEffect(() => setNatural(null), [viewKey]);
  const size = view?.measured ? natural : view ? { width: view.width, height: view.height } : null;
  const asset = view ? assets.find((row) => row.id === view.assetId) : undefined;
  const assetJobs = view ? jobs.filter((job) => job.asset_id === view.assetId) : [];
  const newest = assetJobs.at(-1) ?? null;
  const job = view?.jobId ? (jobs.find((item) => item.id === view.jobId) ?? null) : null;
  const regenerable =
    canRegenerate &&
    view?.modality !== null &&
    asset !== undefined &&
    asset.frozen_at === null &&
    !["released", "dropped", "rejected"].includes(asset.status);

  return (
    <Sheet open={subject !== null} onOpenChange={(open) => (open ? null : onClose())}>
      {view ? (
        <SheetContent title={view.title} description={view.description} className="sm:max-w-4xl">
          <div className="flex flex-col gap-6 px-4 py-4 sm:px-6" data-testid="media-detail-drawer">
            {view.video ? (
              <LetterboxFrame ratio={view.ratio} frameRatio={16 / 9}>
                <video
                  src={mediaContentUrl(view.mediaId, "master")}
                  poster={view.posterId ? mediaContentUrl(view.mediaId, "poster") : undefined}
                  muted
                  playsInline
                  controls
                  preload="metadata"
                  aria-label={`${view.title}, full resolution`}
                  className="block size-full bg-bg"
                />
              </LetterboxFrame>
            ) : (
              <ZoomPan
                key={view.mediaId}
                src={mediaContentUrl(view.mediaId, "master")}
                ratio={view.ratio}
                alt={view.title}
                onNatural={(width, height) => setNatural({ width, height })}
              />
            )}

            <Section title="File">
              <Facts
                rows={[
                  ["Ratio", view.ratioLabel],
                  ["Size", size ? `${pxLabel(`${size.width}x${size.height}`)} px` : "Measured when the file loads"],
                  ["Bytes", view.bytes === null ? "—" : bytesOfLimit(view.bytes, view.maxBytes)],
                  ["Derivation", view.derivation],
                  ...view.extra,
                ]}
              />
            </Section>

            <Section title="Provenance">
              <Facts
                rows={[
                  [
                    "Model",
                    job ? (
                      <span className="inline-flex flex-wrap items-center gap-2">
                        <span className="font-mono text-xs">{job.model_id}</span>
                        <AiGeneratedChip model={job.model_id} />
                      </span>
                    ) : (
                      view.noJobReason
                    ),
                  ],
                  ["Provider", job ? (job.provider_tag ?? "Any provider (routed by OpenRouter)") : "—"],
                  ["Seed", view.seed === null ? "Not recorded for this request" : String(view.seed)],
                  ["Job", job ? <MonoId value={job.id} label="Job id" /> : "—"],
                  [
                    "Cost",
                    job
                      ? `${job.cost_usd ? `${usd(job.cost_usd)} billed` : "Not billed yet"} · ${usd(job.estimate_usd)} estimated`
                      : "—",
                  ],
                ]}
              />
            </Section>

            <Section title="Disclosure, as read back from the file">
              <Disclosure disclosure={view.disclosure} unstampedWhy={view.unstampedWhy} />
            </Section>

            <Section title="Lint">
              {view.lint ? <LintFacts lint={view.lint} /> : <p className="text-sm text-fg-muted">{view.lintNote}</p>}
            </Section>

            <Section title="Lineage">
              <Lineage nodes={view.lineage(renditions)} />
            </Section>

            {regenerable && view.modality ? (
              <GenerationPanel
                runId={runId}
                projectId={projectId}
                assetId={view.assetId}
                modality={view.modality}
                current={newest ? { model_id: newest.model_id, provider_tag: newest.provider_tag } : null}
              />
            ) : null}
          </div>
        </SheetContent>
      ) : null}
    </Sheet>
  );
}

// ---------------------------------------------------------------------------
// what the drawer says about each kind of file
// ---------------------------------------------------------------------------

type LineageNode = { key: string; label: ReactNode; detail?: ReactNode; current?: boolean; children?: LineageNode[] };

type View = {
  title: string;
  description: string;
  mediaId: string;
  assetId: string;
  modality: "image" | "video" | null;
  video: boolean;
  posterId: string | null;
  width: number;
  height: number;
  /** The size is read off the loaded file (a master: 4.4.2 stores no px). */
  measured: boolean;
  ratio: number;
  ratioLabel: string;
  bytes: number | null;
  maxBytes: number | null | undefined;
  derivation: string;
  extra: [string, ReactNode][];
  jobId: string | null;
  noJobReason: string;
  seed: number | null;
  disclosure: Record<string, unknown> | null;
  unstampedWhy: string;
  lint: CandidateLint | null;
  lintNote: string;
  lineage: (renditions: ImageRenditionsOutput | null) => LineageNode[];
};

function candidateNodes(masters: ConceptMasters, children: LineageNode[] = []): LineageNode[] {
  return masters.candidates.map((candidate: Candidate) => {
    const chosen = candidate.media_id === masters.master?.media_id;
    return {
      key: candidate.media_id,
      label: chosen ? "Master" : `Candidate, attempt ${candidate.attempt}`,
      detail: (
        <span className="inline-flex flex-wrap items-center gap-1.5">
          <span className="tabular-nums">seed {candidate.seed ?? "—"}</span>
          <LintChip verdict={candidate.lint.verdict} />
          {chosen ? <span>{masters.master?.why}</span> : null}
        </span>
      ),
      children: chosen ? children : [],
    };
  });
}

function describe(subject: DrawerSubject, allMasters: ConceptMasters[]): View {
  if (subject.kind === "video") {
    const { video, rendition } = subject;
    const size = /^(\d+)x(\d+)$/.exec(rendition.px);
    const width = size ? Number(size[1]) : 16;
    const height = size ? Number(size[2]) : 9;
    return {
      title: `${video.campaign_ref} video · ${rendition.ratio}`,
      description: `The finished master, ${secondsLabel(rendition.duration_ms)}. The library plays its 480p proxy.`,
      mediaId: rendition.media_id,
      assetId: video.asset_id,
      modality: "video",
      video: true,
      posterId: rendition.poster_media_id,
      width,
      height,
      measured: false,
      ratio: width / height,
      ratioLabel: rendition.ratio,
      bytes: rendition.bytes,
      maxBytes: undefined,
      derivation: rendition.derivation === "native" ? "Native" : `Cropped from ${rendition.source_ratio}`,
      extra: [
        ["Duration", secondsLabel(rendition.duration_ms)],
        ["Audio", rendition.has_audio ? "Sound, loudness-normalised" : "Silent AAC track"],
        ["Verification", rendition.verification.passed ? "Passed" : rendition.verification.failures.join("; ")],
        ["Fast start", rendition.verification.faststart ? "Yes (+faststart)" : "No"],
      ],
      jobId: video.clips[0]?.job_id ?? null,
      noJobReason: "Assembled from clips; see the lineage",
      seed: null,
      disclosure: rendition.disclosure,
      unstampedWhy: "This file carries no disclosure record.",
      lint: null,
      lintNote: "A video is verified, not image-linted: see Verification above and the timeline in the library.",
      lineage: () => [
        {
          key: "clips",
          label: `${video.clips.length} generated ${video.clips.length === 1 ? "clip" : "clips"}`,
          detail: video.clips.map((clip) => `#${clip.index + 1} ${clip.ratio} ${clip.duration_s} s`).join(" · "),
          children: [
            {
              key: rendition.media_id,
              label: `Master ${rendition.ratio}`,
              current: true,
              detail: "Assembled, captioned, end card, loudness, encoded, stamped",
              children: [
                { key: rendition.preview_media_id, label: "480p proxy", detail: "What the library plays" },
                { key: rendition.poster_media_id, label: "Poster frame", detail: "At the brand’s first frame" },
              ],
            },
          ],
        },
      ],
    };
  }

  if (subject.kind === "master") {
    const { concept, masters } = subject;
    const master = masters.master;
    const winner = masters.candidates.find((item) => item.media_id === master?.media_id) ?? null;
    const ratio = ratioValue(masters.aspect_ratio ?? "") ?? 1;
    return {
      title: `${concept.name} · master`,
      description: "The candidate 4.4.2 chose; every rendition of this concept is made from it or beside it.",
      mediaId: master?.media_id ?? "",
      assetId: masters.asset_id,
      modality: "image",
      video: false,
      posterId: null,
      width: 0,
      height: 0,
      measured: true,
      ratio,
      ratioLabel: masters.aspect_ratio ?? "not recorded",
      bytes: null,
      maxBytes: undefined,
      derivation: "Native (as painted)",
      extra: [["Why this one", master?.why ?? "—"]],
      jobId: winner?.job_id ?? null,
      noJobReason: "—",
      seed: winner?.seed ?? null,
      disclosure: null,
      unstampedWhy:
        "A master is not stamped: it is never shipped. Each rendition made from it is stamped when it is written.",
      lint: winner?.lint ?? null,
      lintNote: "No lint was recorded for this master.",
      lineage: (renditions) =>
        candidateNodes(
          masters,
          (renditions?.renditions ?? [])
            .filter((item) => item.concept_id === concept.id)
            .map((item) => ({
              key: item.media_id,
              label: `${item.ratio} rendition`,
              detail: DERIVATION_LABEL[item.derivation],
            })),
        ).map((node) => (node.key === master?.media_id ? { ...node, current: true } : node)),
    };
  }

  const { tile } = subject;
  const masters = allMasters.find((item) => item.concept_id === tile.conceptId) ?? null;
  const rendition = tile.rendition;
  const fromMaster = rendition ? rendition.derivation !== "native" || rendition.job_id === null : false;
  const winner = masters?.candidates.find((item) => item.media_id === masters.master?.media_id) ?? null;
  const self: LineageNode = {
    key: tile.mediaId,
    label: `${tile.ratio} ${tile.logo ? "logo slot" : "rendition"}`,
    current: true,
    detail: DERIVATION_LABEL[tile.derivation],
    children: [{ key: `${tile.mediaId}:preview`, label: "WebP proxy", detail: "What the grid shows" }],
  };
  return {
    title: `${tile.conceptName ?? (tile.logo ? "Logo" : tile.campaignRef)} · ${tile.ratio}`,
    description: tile.logo
      ? "A registered logo fitted to the spec sheet's slot by padding — never generated."
      : "The file as it ships, loaded at full resolution.",
    mediaId: tile.mediaId,
    assetId: tile.assetId,
    modality: tile.generated ? "image" : null,
    video: false,
    posterId: null,
    width: tile.width,
    height: tile.height,
    measured: false,
    ratio: tile.width / tile.height,
    ratioLabel: tile.ratio,
    bytes: tile.bytes,
    maxBytes: tile.maxBytes,
    derivation: DERIVATION_LABEL[tile.derivation],
    extra: [
      [
        "Scale",
        `sx = sy = ${(rendition?.scale.sx ?? tile.logo?.scale.sx ?? 1).toFixed(4)}`,
      ],
      ...(rendition && rendition.derivation === "crop"
        ? ([["Saliency kept", `${Math.round(rendition.retained_saliency * 100)}%`]] as [string, ReactNode][])
        : []),
      ...(rendition
        ? ([
            [
              "Logo",
              rendition.logo_composited ? "Composited by code" : (rendition.logo_note ?? "Not composited"),
            ],
          ] as [string, ReactNode][])
        : []),
    ],
    jobId: tile.jobId,
    noJobReason: tile.logo ? "None — a registered logo" : "—",
    seed: fromMaster ? (winner?.seed ?? null) : null,
    disclosure: rendition ? rendition.disclosure : null,
    unstampedWhy: "Nobody generated a registered logo, so there is nothing to disclose.",
    lint: tile.rendition?.lint ?? tile.logo?.lint ?? null,
    lintNote: "No lint was recorded for this file.",
    lineage: () => {
      if (tile.logo) {
        return [
          {
            key: tile.logo.registered_logo_id,
            label: "Registered logo",
            detail: <MonoId value={tile.logo.registered_logo_id} label="Registered logo id" />,
            children: [self],
          },
        ];
      }
      if (!masters) return [self];
      if (!fromMaster) {
        return [
          ...candidateNodes(masters),
          {
            key: `painted:${tile.mediaId}`,
            label: `Painted natively at ${tile.ratio}`,
            detail: "A fresh frame from the model, not a copy of the master",
            children: [self],
          },
        ];
      }
      return candidateNodes(masters, [self]);
    },
  };
}

// ---------------------------------------------------------------------------
// pieces
// ---------------------------------------------------------------------------

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <h3 className="text-sm font-semibold text-fg">{title}</h3>
      {children}
    </section>
  );
}

function Facts({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
      {rows.map(([term, value]) => (
        <div key={term} className="flex min-w-0 gap-3">
          <dt className="w-28 shrink-0 text-fg-muted">{term}</dt>
          <dd className="min-w-0 break-words tabular-nums text-fg">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function Disclosure({
  disclosure,
  unstampedWhy,
}: {
  disclosure: Record<string, unknown> | null;
  unstampedWhy: string;
}) {
  if (!disclosure) return <p className="text-sm text-fg-muted">{unstampedWhy}</p>;
  const source = typeof disclosure["xmp_digital_source_type"] === "string" ? disclosure["xmp_digital_source_type"] : null;
  const labels = Array.isArray(disclosure["visible_labels"]) ? (disclosure["visible_labels"] as unknown[]) : [];
  const comment = typeof disclosure["mp4_comment"] === "string" ? disclosure["mp4_comment"] : null;
  return (
    <Facts
      rows={[
        [
          "Source type",
          source ? (
            <span className="flex flex-col gap-0.5">
              <span>{SOURCE_TYPE[source] ?? "Unrecognised value"}</span>
              <span className="break-all font-mono text-xs text-fg-subtle">{source}</span>
            </span>
          ) : (
            "No XMP DigitalSourceType was read back"
          ),
        ],
        ...(comment !== null ? ([["MP4 comment", <span key="c" className="font-mono text-xs">{comment}</span>]] as [string, ReactNode][]) : []),
        [
          "Visible labels",
          labels.length === 0 ? "None required on this surface" : labels.map((label) => String(label)).join(", "),
        ],
      ]}
    />
  );
}

function LintFacts({ lint }: { lint: CandidateLint }) {
  return (
    <div className="flex flex-col gap-2 text-sm">
      <p className="flex flex-wrap items-center gap-2">
        <LintChip verdict={lint.verdict} />
        <span className="text-fg-muted">at ruleset {lint.ruleset_version}</span>
        {lint.unchecked ? <Badge tone="warning">Some rules could not be measured</Badge> : null}
      </p>
      {lint.rule_ids.length === 0 ? (
        <p className="text-fg-muted">No rule fired.</p>
      ) : (
        <ul className="flex flex-wrap gap-1.5" aria-label="Rules that fired">
          {lint.rule_ids.map((rule) => (
            <li key={rule} className="rounded-full border px-2 py-px font-mono text-xs text-fg">
              {rule}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Lineage({ nodes }: { nodes: LineageNode[] }) {
  return (
    <ul role="tree" aria-label="Lineage" className="flex flex-col gap-1 text-sm">
      {nodes.map((node) => (
        <LineageItem key={node.key} node={node} depth={0} />
      ))}
    </ul>
  );
}

function LineageItem({ node, depth }: { node: LineageNode; depth: number }) {
  return (
    <li
      role="treeitem"
      aria-level={depth + 1}
      aria-selected={node.current ? true : undefined}
      aria-expanded={node.children && node.children.length > 0 ? true : undefined}
      className="flex flex-col gap-1"
    >
      <span className={cn("flex flex-wrap items-baseline gap-x-2 gap-y-1", node.current ? "font-semibold text-fg" : "text-fg")}>
        {node.label}
        {node.current ? <Badge tone="accent">This file</Badge> : null}
        {node.detail ? <span className="text-xs font-normal text-fg-muted">{node.detail}</span> : null}
      </span>
      {node.children && node.children.length > 0 ? (
        <ul role="group" className="ml-2 flex flex-col gap-1 border-l pl-4">
          {node.children.map((child) => (
            <LineageItem key={child.key} node={child} depth={depth + 1} />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

/**
 * Fit, or 100%: one image pixel to one CSS pixel, panned by dragging, by the
 * scrollbars or — the frame takes focus — by the arrow keys. The size at 100%
 * is the loaded file's own.
 */
function ZoomPan({
  src,
  ratio,
  alt,
  onNatural,
}: {
  src: string;
  ratio: number;
  alt: string;
  onNatural: (width: number, height: number) => void;
}) {
  const [zoom, setZoom] = useState<"fit" | "actual">("fit");
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  const frame = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; y: number; left: number; top: number } | null>(null);

  const onLoad = (event: SyntheticEvent<HTMLImageElement>) => {
    const { naturalWidth, naturalHeight } = event.currentTarget;
    setSize({ width: naturalWidth, height: naturalHeight });
    onNatural(naturalWidth, naturalHeight);
  };
  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    const node = frame.current;
    if (zoom !== "actual" || !node) return;
    drag.current = { x: event.clientX, y: event.clientY, left: node.scrollLeft, top: node.scrollTop };
    node.setPointerCapture(event.pointerId);
  };
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const node = frame.current;
    const start = drag.current;
    if (!node || !start) return;
    node.scrollLeft = start.left - (event.clientX - start.x);
    node.scrollTop = start.top - (event.clientY - start.y);
  };

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <SegmentedControl
          label="Zoom"
          value={zoom}
          onChange={setZoom}
          options={[
            { value: "fit", label: "Fit" },
            { value: "actual", label: "100%" },
          ]}
        />
        <span className="text-xs tabular-nums text-fg-muted">
          {zoom === "actual"
            ? "100% · drag or use the arrow keys to pan"
            : size
              ? `Fitted · ${size.width} × ${size.height} px at 100%`
              : "Loading the full-resolution file"}
        </span>
      </div>
      {zoom === "fit" ? (
        <LetterboxFrame ratio={ratio} frameRatio={4 / 3}>
          {/* eslint-disable-next-line @next/next/no-img-element -- the signed master; next/image would re-encode it */}
          <img
            src={src}
            alt={alt}
            decoding="async"
            onLoad={onLoad}
            data-testid="drawer-master"
            className="block size-full object-contain"
          />
        </LetterboxFrame>
      ) : (
        <div
          ref={frame}
          tabIndex={0}
          role="region"
          aria-label={`${alt} at 100%, scrollable`}
          data-testid="zoom-actual"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={() => {
            drag.current = null;
          }}
          className={cn(
            "h-96 cursor-grab touch-none overflow-auto rounded-token border bg-surface active:cursor-grabbing",
            "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
          )}
        >
          {/* eslint-disable-next-line @next/next/no-img-element -- the signed master at its own pixel size */}
          <img
            src={src}
            alt=""
            draggable={false}
            decoding="async"
            onLoad={onLoad}
            className="block max-w-none select-none"
          />
        </div>
      )}
    </div>
  );
}
