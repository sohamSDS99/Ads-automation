"use client";

import { ArrowLeft, FileClock, Keyboard } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { DescriptionTable } from "@/components/creative/description-table";
import { HeadlineMatrix } from "@/components/creative/headline-matrix";
import { PairHeatmap } from "@/components/creative/pair-heatmap";
import { SerpPreview, type Device, type SerpLine } from "@/components/creative/serp-preview";
import { VariantCompare } from "@/components/creative/variant-compare";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { Popover } from "@/components/ui/popover";
import { SegmentedControl } from "@/components/ui/segmented";
import { Skeleton } from "@/components/ui/skeleton";
import type {
  ClaimBoundDescriptionsOutput,
  ClaimRef,
  CombinationCoherenceOutput,
  CreativeAssetItem,
  HeadlineSpreadOutput,
  VariantBOutput,
} from "@/lib/api/creative-runs";
import { adSlots, combination, position, studioAd, type StudioAd, type Variant } from "@/lib/creative/ad-studio";
import {
  errorMessage,
  useCreativeAssets,
  useCreativeBrief,
  useCreativeOverview,
  useNodeRun,
  usePinnedRuleSet,
} from "@/lib/queries";
import { useSession } from "@/lib/session";
import { cn } from "@/lib/utils";

const SHORTCUTS: [keys: string, what: string][] = [
  ["Enter", "Save the headline or description being edited"],
  ["Esc", "Put the stored text back"],
  ["Tab", "Next control; the pair grids are one stop each"],
  ["← ↑ → ↓", "Move within a pair grid"],
  ["Enter", "Load the focused pair into the preview"],
  ["Enter / Space", "Open a reserve’s Swap in menu; arrows and Enter pick what it replaces"],
  ["?", "Show these shortcuts"],
];

/**
 * The Ad Studio (Stage 04 PRD §15.3 `…/runs/[runId]/ads`, §15.4 E): the ad
 * groups on the left; in the centre the selected combination in a SERP
 * preview, the headline matrix, the pair heatmap, the descriptions and the
 * A/B comparison.
 *
 * It renders what the run wrote (`GET /creative-runs/{id}/assets`) beside what
 * 4.2.1–4.2.4 judged (their stored outputs), limits from the spec sheet at the
 * run's pin, and claims from the brief. Every verdict on screen is the
 * server's; editing and swapping are for `creative_execute` only and are
 * absent, not disabled, for everyone else (§15.5 item 4).
 */
export function AdStudio({ projectId, runId }: { projectId: string; runId: string }) {
  const session = useSession();
  const editable = session.has("creative_execute");
  const brief = useCreativeBrief(runId);
  const assets = useCreativeAssets(runId);
  const overview = useCreativeOverview(projectId);
  const spread = useNodeRun(runId, "4.2.1");
  const descriptions = useNodeRun(runId, "4.2.2");
  const coherence = useNodeRun(runId, "4.2.3");
  const variantB = useNodeRun(runId, "4.2.4");

  const pins = overview.data?.runs.find((run) => run.run_id === runId)?.pins ?? [];
  const pin = pins.at(-1)?.["ruleset_version"] ?? brief.data?.brief.ruleset_ref.ruleset_version ?? null;
  const ruleset = usePinnedRuleSet(projectId, pin);
  const search = ruleset.data?.compiled.asset_specs?.specs?.["search"];
  const limits = {
    headline: search?.["headline"]?.max_chars ?? null,
    description: search?.["description"]?.max_chars ?? null,
  };

  const outputs = useMemo(
    () => ({
      spread: (spread.data?.output ?? null) as HeadlineSpreadOutput | null,
      descriptions: (descriptions.data?.output ?? null) as ClaimBoundDescriptionsOutput | null,
      coherence: (coherence.data?.output ?? null) as CombinationCoherenceOutput | null,
      variantB: (variantB.data?.output ?? null) as VariantBOutput | null,
    }),
    [spread.data, descriptions.data, coherence.data, variantB.data],
  );
  const slots = useMemo(() => adSlots(outputs.spread, outputs.variantB), [outputs]);
  const [slotKey, setSlotKey] = useState<string | null>(null);
  const [variant, setVariant] = useState<Variant>("A");
  const [device, setDevice] = useState<Device>("desktop");
  const [focus, setFocus] = useState<{ ids: string[]; caption: string } | null>(null);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);

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

  const slot = slots.find((s) => s.key === slotKey) ?? slots[0] ?? null;
  const rows: CreativeAssetItem[] = assets.data?.items ?? [];
  const groups = brief.data?.brief.ad_groups ?? [];
  const ad: StudioAd | null = slot ? studioAd(slot, variant, outputs, rows, groups) : null;
  const adA: StudioAd | null = slot ? studioAd(slot, "A", outputs, rows, groups) : null;
  const adB: StudioAd | null = slot?.variants.includes("B") ? studioAd(slot, "B", outputs, rows, groups) : null;
  const claims = useMemo(
    () => new Map<string, ClaimRef>((brief.data?.brief.proof_points ?? []).map((claim) => [claim.claim_id, claim])),
    [brief.data],
  );

  const back = (
    <Link
      href={`/projects/${projectId}/creative/runs/${runId}`}
      className="inline-flex w-fit items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
    >
      <ArrowLeft className="size-4 shrink-0" aria-hidden />
      Back to the Creative Console
    </Link>
  );

  if (assets.isPending || spread.isPending || brief.isPending) {
    return (
      <div className="flex flex-col gap-6">
        {back}
        <div className="flex flex-col gap-6 lg:flex-row">
          <Skeleton className="h-48 w-full lg:w-60" />
          <div className="flex min-w-0 flex-1 flex-col gap-6">
            <Skeleton className="h-44 w-full max-w-2xl" />
            <Skeleton className="h-96 w-full" />
          </div>
        </div>
      </div>
    );
  }

  if (!outputs.spread || slots.length === 0) {
    return (
      <div className="flex flex-col gap-6">
        {back}
        <EmptyState
          icon={FileClock}
          title="No ads are written yet"
          description="Node 4.2.1 writes each Search ad group’s headlines once the brief is approved at G7, and 4.2.2–4.2.4 add descriptions, pair checks and variant B. They appear here as each node finishes."
          action={
            <Link
              href={`/projects/${projectId}/creative/runs/${runId}/brief`}
              className="text-sm text-accent hover:underline"
            >
              Open the brief
            </Link>
          }
        />
      </div>
    );
  }

  const failed = [assets, brief].map(errorMessage).find(Boolean);
  if (failed || !ad || !adA || !slot) {
    return (
      <div className="flex flex-col gap-6">
        {back}
        <Alert tone="error" title="The Ad Studio could not be loaded">
          {failed ?? "This ad group’s copy could not be read from the run. Refresh the page to try again."}
        </Alert>
      </div>
    );
  }

  const scope = { campaign_type: slot.campaign_type, market: slot.market, language: slot.language };
  const shown = combination(ad, focus?.ids ?? []);
  const line = (asset: CreativeAssetItem): SerpLine => ({
    id: asset.id,
    label: `${position(ad, asset)}`,
    text: asset.text ?? "",
    surface: asset.surface,
  });
  const choose = (next: { slot?: string; variant?: Variant }) => {
    if (next.slot !== undefined) setSlotKey(next.slot);
    if (next.variant !== undefined) setVariant(next.variant);
    setFocus(null);
  };

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        {back}
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

      <div className="flex flex-col gap-6 lg:flex-row lg:items-start">
        <nav aria-label="Ad groups" className="w-full shrink-0 lg:sticky lg:top-6 lg:w-60">
          <h1 className="mb-2 text-lg font-medium tracking-tight text-fg">Ad Studio</h1>
          <ul className="flex flex-col gap-0.5">
            {slots.map((item) => {
              const current = item.key === slot.key;
              return (
                <li key={item.key}>
                  <button
                    type="button"
                    aria-current={current ? "true" : undefined}
                    onClick={() => choose({ slot: item.key })}
                    className={cn(
                      "flex w-full flex-col items-start rounded-token border px-3 py-2 text-left transition-colors duration-150 motion-reduce:transition-none",
                      "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
                      current ? "border-border-strong bg-surface-raised" : "border-transparent hover:bg-surface-hover",
                    )}
                  >
                    <span className={cn("text-sm", current ? "font-medium text-fg" : "text-fg")}>{item.ad_group_ref}</span>
                    <span className="text-xs text-fg-muted">
                      {item.campaign_ref} · {item.market} · {item.variants.length === 2 ? "A and B" : "A"}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
          {pin ? (
            <p className="mt-3 text-xs text-fg-muted">
              Linted against ruleset <span className="font-mono text-fg">{pin}</span>
            </p>
          ) : null}
        </nav>

        <div className="flex min-w-0 flex-1 flex-col gap-10">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <h2 className="text-lg font-medium tracking-tight text-fg">{slot.ad_group_ref}</h2>
              <p className="text-sm text-fg-muted">
                {slot.campaign_ref} · {slot.market} · {slot.language}
              </p>
            </div>
            {slot.variants.length > 1 ? (
              <SegmentedControl<Variant>
                label="Variant"
                value={variant}
                onChange={(value) => choose({ variant: value })}
                options={[
                  { value: "A", label: "Variant A" },
                  { value: "B", label: "Variant B" },
                ]}
              />
            ) : null}
          </div>

          <SerpPreview
            device={device}
            onDevice={setDevice}
            headlines={shown.headlines.map(line)}
            descriptions={shown.descriptions.map(line)}
            finalUrl={ad.finalUrl}
            paths={ad.paths}
            caption={
              focus?.caption ??
              `Variant ${variant}: the first combination Google can serve, pins in their positions.`
            }
          />

          <HeadlineMatrix
            runId={runId}
            ad={ad}
            limit={limits.headline}
            claims={claims}
            scope={scope}
            editable={editable}
            onPreview={(id) => {
              const asset = ad.headlines.find((row) => row.id === id);
              setFocus({ ids: [id], caption: `Showing ${asset ? position(ad, asset) : "the headline"} first.` });
            }}
          />

          <PairHeatmap
            ad={ad}
            selected={focus && focus.ids.length === 2 ? [focus.ids[0] as string, focus.ids[1] as string] : null}
            onSelect={(a, b) => {
              const first = [...ad.headlines, ...ad.descriptions].find((row) => row.id === a);
              const second = [...ad.headlines, ...ad.descriptions].find((row) => row.id === b);
              setFocus({
                ids: [a, b],
                caption: `${first ? position(ad, first) : "One"} with ${second ? position(ad, second) : "the other"}, from the pair grid.`,
              });
              document.getElementById("serp-preview-title")?.scrollIntoView({ block: "nearest" });
            }}
          />

          <DescriptionTable
            runId={runId}
            ad={ad}
            limit={limits.description}
            claims={claims}
            scope={scope}
            editable={editable}
          />

          <VariantCompare a={adA} b={adB} brief={groups.find((g) => g.ad_group_ref === slot.ad_group_ref && g.campaign_ref === slot.campaign_ref) ?? null} />
        </div>
      </div>
    </div>
  );
}
