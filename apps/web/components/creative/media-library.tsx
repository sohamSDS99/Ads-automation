"use client";

import { ArrowLeft, Clapperboard, Images, Keyboard, Layers, ListChecks } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ConceptBoard } from "@/components/creative/concept-board";
import { JobStatusList } from "@/components/creative/job-status-list";
import { CheckerDefs } from "@/components/creative/media-frame";
import { MediaDetailDrawer, type DrawerSubject } from "@/components/creative/media-detail-drawer";
import { RenditionGrid } from "@/components/creative/rendition-grid";
import { SpendMeters } from "@/components/creative/spend-meters";
import { VideoPlayer } from "@/components/creative/video-player";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { Popover } from "@/components/ui/popover";
import { SegmentedControl } from "@/components/ui/segmented";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs } from "@/components/ui/tabs";
import { ApiError } from "@/lib/api";
import type {
  CampaignVideo,
  CreativeConceptsOutput,
  ImageMastersOutput,
  ImageRenditionsOutput,
  VideoProductionOutput,
} from "@/lib/api/media-library";
import { boardConcepts, surfaceGroups, type FileTile } from "@/lib/creative/media-library";
import {
  errorMessage,
  useCreativeAssets,
  useCreativeBrief,
  useGenerationJobs,
  useGuideline,
  useNodeRun,
  useProject,
  useRun,
} from "@/lib/queries";
import { useSession } from "@/lib/session";

type Tab = "concepts" | "renditions" | "video" | "jobs";
const TABS: Tab[] = ["concepts", "renditions", "video", "jobs"];

/** The creative console's spend-meter cadence: media moves without an SSE event. */
const SPEND_POLL_MS = 10_000;

const SHORTCUTS: [keys: string, what: string][] = [
  ["← → Home End", "Move between the tabs"],
  ["Tab / Enter", "Move between tiles; open one in the detail view"],
  ["Esc", "Close the detail view"],
  ["← → ↑ ↓", "On the video timeline: seek one second"],
  ["Page Up / Down", "On the video timeline: seek five seconds"],
  ["Home / End", "On the video timeline: the start or the end"],
  ["Arrow keys", "At 100% zoom: pan the image"],
  ["?", "Show these shortcuts"],
];

/** A node that has not run yet answers 404: that is "not yet", not an error. */
function notYet(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/**
 * The Media Library (Stage 04 PRD §15.3 `…/runs/[runId]/media`, §15.4 G):
 * the run's concepts with their masters, every rendition as an aspect-true
 * tile grouped by surface, the finished videos on their verified timeline,
 * and every generation job. Files open in the detail drawer — the only place
 * a master loads — which carries regeneration for `creative_execute`.
 *
 * Everything shown is what 4.4.1–4.4.4 stored and what the api says of it; no
 * verdict is re-derived here.
 */
export function MediaLibrary({ projectId, runId }: { projectId: string; runId: string }) {
  const session = useSession();
  const canRegenerate = session.has("creative_execute");
  const project = useProject(projectId);
  const run = useRun(runId, { pollMs: SPEND_POLL_MS });
  const brief = useCreativeBrief(runId);
  const conceptsRun = useNodeRun(runId, "4.4.1");
  const mastersRun = useNodeRun(runId, "4.4.2");
  const renditionsRun = useNodeRun(runId, "4.4.3");
  const videoRun = useNodeRun(runId, "4.4.4");
  const assets = useCreativeAssets(runId);
  const jobs = useGenerationJobs(runId);
  const guideline = useGuideline(brief.data?.brief.ruleset_ref.guideline_id ?? null);

  const [tab, setTab] = useState<Tab>("concepts");
  const [subject, setSubject] = useState<DrawerSubject | null>(null);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);

  // `?tab=renditions` opens that tab: a link can point at the grid.
  useEffect(() => {
    const wanted = new URLSearchParams(window.location.search).get("tab");
    if (wanted && (TABS as string[]).includes(wanted)) setTab(wanted as Tab);
  }, []);
  const choose = (next: string) => {
    setTab(next as Tab);
    const url = new URL(window.location.href);
    url.searchParams.set("tab", next);
    window.history.replaceState(null, "", url);
    scroller.current?.scrollTo({ top: 0 });
  };

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (event.key !== "?" || target?.closest("input, textarea, select, [contenteditable]")) return;
      event.preventDefault();
      setShortcutsOpen((open) => !open);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const concepts = (conceptsRun.data?.output ?? null) as CreativeConceptsOutput | null;
  const masters = (mastersRun.data?.output ?? null) as ImageMastersOutput | null;
  const renditions = (renditionsRun.data?.output ?? null) as ImageRenditionsOutput | null;
  const video = (videoRun.data?.output ?? null) as VideoProductionOutput | null;

  const board = useMemo(() => boardConcepts(concepts), [concepts]);
  const groups = useMemo(() => surfaceGroups(renditions, concepts), [renditions, concepts]);
  const jobRows = useMemo(() => jobs.data?.items ?? [], [jobs.data]);
  const models = useMemo(() => new Map(jobRows.map((job) => [job.id, job.model_id])), [jobRows]);
  const palette = useMemo(() => {
    const colour = guideline.data?.payload?.brand_rules?.visual_identity?.colour;
    const tokens = colour && Array.isArray(colour["tokens"]) ? (colour["tokens"] as unknown[]) : [];
    return tokens.filter((token): token is { name?: unknown; hex?: unknown } => typeof token === "object" && token !== null);
  }, [guideline.data]);

  const fileCount = groups.reduce((sum, group) => sum + group.files, 0);
  const gapCount = groups.reduce((sum, group) => sum + group.gaps, 0);
  const videoCount = video?.videos.reduce((sum, item) => sum + item.renditions.length, 0) ?? 0;

  const openTile = useCallback((tile: FileTile) => setSubject({ kind: "file", tile }), []);

  const assetLabel = useCallback(
    (assetId: string) => {
      const concept = masters?.concepts.find((item) => item.asset_id === assetId);
      if (concept) return `${board.find((c) => c.id === concept.concept_id)?.name ?? concept.concept_id} image`;
      const clip = video?.videos.find((item) => item.asset_id === assetId);
      if (clip) return `${clip.campaign_ref} video`;
      return null;
    },
    [masters, video, board],
  );
  const openAsset = useCallback(
    (assetId: string) => {
      const concept = masters?.concepts.find((item) => item.asset_id === assetId);
      const onBoard = concept ? board.find((c) => c.id === concept.concept_id) : undefined;
      if (concept?.master && onBoard) {
        setSubject({ kind: "master", concept: onBoard, masters: concept });
        return;
      }
      const clip = video?.videos.find((item) => item.asset_id === assetId);
      const first = clip?.renditions[0];
      if (clip && first) setSubject({ kind: "video", video: clip, rendition: first });
    },
    [masters, video, board],
  );

  const home = `/projects/${projectId}/creative/runs/${runId}`;
  const briefHref = `${home}/brief`;
  const loadError = [conceptsRun, mastersRun, renditionsRun, videoRun].find(
    (query) => query.isError && !notYet(query.error),
  );

  return (
    <div className="flex h-main min-h-0 flex-col gap-3">
      <CheckerDefs />
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Link
          href={home}
          className="inline-flex w-fit items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
        >
          <ArrowLeft className="size-4 shrink-0" aria-hidden />
          <span className="truncate">{project.data?.name ?? "Creative console"} · console</span>
        </Link>
        <Popover
          open={shortcutsOpen}
          onOpenChange={setShortcutsOpen}
          side="bottom"
          align="end"
          trigger={
            <Button variant="ghost" size="sm" aria-keyshortcuts="?">
              <Keyboard aria-hidden />
              Keyboard shortcuts
            </Button>
          }
        >
          <table className="w-full text-sm">
            <caption className="sr-only">Keyboard shortcuts</caption>
            <tbody>
              {SHORTCUTS.map(([keys, what]) => (
                <tr key={`${keys}-${what}`} className="align-top">
                  <th scope="row" className="whitespace-nowrap py-1 pr-3 text-left font-mono text-xs font-normal text-fg">
                    {keys}
                  </th>
                  <td className="py-1 text-fg-muted">{what}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Popover>
      </div>

      <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
        <div className="flex min-w-0 flex-col gap-1">
          <h1 className="text-xl font-semibold tracking-tight text-fg">Media library</h1>
          <p className="text-sm tabular-nums text-fg-muted">
            {board.length} {board.length === 1 ? "concept" : "concepts"} · {fileCount}{" "}
            {fileCount === 1 ? "file" : "files"}
            {gapCount > 0 ? ` · ${gapCount} ${gapCount === 1 ? "gap" : "gaps"}` : ""} · {videoCount}{" "}
            {videoCount === 1 ? "video" : "videos"} · {jobRows.length} {jobRows.length === 1 ? "job" : "jobs"}
          </p>
        </div>
        <SpendMeters spend={run.data?.creative_spend} />
      </header>

      {loadError ? (
        <Alert tone="error" title="Part of the library could not be loaded">
          {errorMessage(loadError)} Reload the page; if it persists, the run&apos;s node outputs are unreadable.
        </Alert>
      ) : null}

      <Tabs
        label="Media library"
        value={tab}
        onChange={choose}
        items={[
          { id: "concepts", label: "Concepts", badge: String(board.length) },
          { id: "renditions", label: "Renditions", badge: String(fileCount + gapCount) },
          { id: "video", label: "Video", badge: String(videoCount) },
          { id: "jobs", label: "Jobs", badge: String(jobRows.length) },
        ]}
      />

      <div ref={scroller} role="tabpanel" aria-label={tab} className="min-h-0 flex-1 overflow-y-auto pb-6" data-testid="media-panel">
        {tab === "concepts" ? (
          conceptsRun.isPending || mastersRun.isPending ? (
            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-96 w-full" />
              ))}
            </div>
          ) : board.length === 0 ? (
            <EmptyState
              icon={Layers}
              title="No concepts yet"
              description="4.4.1 writes the concepts once the brief is approved at G7, on a run started with images or video on."
              action={
                <Link href={briefHref} className="text-sm font-medium text-accent underline-offset-2 hover:underline">
                  Open the brief
                </Link>
              }
            />
          ) : (
            <ConceptBoard
              concepts={board}
              masters={masters?.concepts ?? []}
              palette={palette}
              briefHref={briefHref}
              onOpenMaster={(concept, item) => setSubject({ kind: "master", concept, masters: item })}
            />
          )
        ) : null}

        {tab === "renditions" ? (
          renditionsRun.isPending ? (
            <Skeleton className="h-96 w-full" />
          ) : groups.length === 0 ? (
            <EmptyState
              icon={Images}
              title="No renditions yet"
              description="4.4.3 makes every ratio the spec sheet requires from each concept's master — native, relaid or a saliency crop — and records a gap where it cannot."
              action={
                <Link href={home} className="text-sm font-medium text-accent underline-offset-2 hover:underline">
                  Follow the run in the console
                </Link>
              }
            />
          ) : (
            <RenditionGrid groups={groups} scrollRef={scroller} models={models} onOpen={openTile} />
          )
        ) : null}

        {tab === "video" ? (
          videoRun.isPending ? (
            <Skeleton className="h-96 w-full" />
          ) : !video || video.videos.length === 0 ? (
            <EmptyState
              icon={Clapperboard}
              title={video?.status === "not_required" ? "No video on this run" : "No finished video yet"}
              description={
                video?.why ??
                "4.4.4 assembles, captions and verifies each campaign's video after its clips are generated."
              }
            />
          ) : (
            <div className="flex flex-col gap-10">
              {video.videos.map((item) => (
                <VideoSection
                  key={item.asset_id}
                  video={item}
                  conceptName={board.find((c) => c.id === item.concept_id)?.name ?? item.concept_id}
                  onOpen={(rendition) => setSubject({ kind: "video", video: item, rendition })}
                />
              ))}
              {video.gaps.length > 0 ? (
                <section className="flex flex-col gap-2" aria-labelledby="video-gaps">
                  <h2 id="video-gaps" className="text-sm font-semibold text-fg">
                    Recorded video gaps
                  </h2>
                  <ul className="flex flex-col gap-1 text-sm">
                    {video.gaps.map((gap, i) => (
                      <li key={`${gap.campaign_ref}-${gap.ratio}-${i}`} className="rounded-token border border-dashed px-3 py-2">
                        <span className="font-medium text-fg">
                          {gap.campaign_ref}
                          {gap.ratio ? ` · ${gap.ratio}` : ""}
                        </span>{" "}
                        <span className="text-fg-muted">{gap.detail}</span>
                      </li>
                    ))}
                  </ul>
                </section>
              ) : null}
            </div>
          )
        ) : null}

        {tab === "jobs" ? (
          jobs.isPending ? (
            <Skeleton className="h-64 w-full" />
          ) : jobs.isError ? (
            <Alert tone="error" title="The generation jobs could not be loaded">
              {errorMessage(jobs)}
            </Alert>
          ) : (
            <JobStatusList runId={runId} jobs={jobRows} assetLabel={assetLabel} onOpenAsset={openAsset} />
          )
        ) : null}
      </div>

      <MediaDetailDrawer
        subject={subject}
        onClose={() => setSubject(null)}
        runId={runId}
        projectId={projectId}
        jobs={jobRows}
        masters={masters?.concepts ?? []}
        renditions={renditions}
        assets={assets.data?.items ?? []}
        canRegenerate={canRegenerate}
      />
    </div>
  );
}

function VideoSection({
  video,
  conceptName,
  onOpen,
}: {
  video: CampaignVideo;
  conceptName: string;
  onOpen: (rendition: CampaignVideo["renditions"][number]) => void;
}) {
  const [ratio, setRatio] = useState(video.renditions[0]?.ratio ?? "");
  const rendition = video.renditions.find((item) => item.ratio === ratio) ?? video.renditions[0];
  if (!rendition) return null;
  return (
    <section className="flex flex-col gap-3" aria-label={`${video.campaign_ref} video`} data-video={video.asset_id}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-0.5">
          <h2 className="text-md font-semibold text-fg">{video.campaign_ref}</h2>
          <p className="text-xs text-fg-muted">
            From “{conceptName}” · {video.clips.length} {video.clips.length === 1 ? "clip" : "clips"} ·{" "}
            {video.duration_s} s script
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {video.renditions.length > 1 ? (
            <SegmentedControl
              label="Ratio"
              value={rendition.ratio}
              onChange={setRatio}
              options={video.renditions.map((item) => ({ value: item.ratio, label: item.ratio }))}
            />
          ) : null}
          <Button variant="secondary" size="sm" onClick={() => onOpen(rendition)}>
            <ListChecks aria-hidden />
            Open the master
          </Button>
        </div>
      </div>
      <VideoPlayer key={rendition.media_id} video={video} rendition={rendition} />
    </section>
  );
}
