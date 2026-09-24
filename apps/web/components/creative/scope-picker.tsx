"use client";

import { useId } from "react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { SegmentedControl } from "@/components/ui/segmented";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";

export type PlanCampaign = { campaign_ref: string; name: string; type: string | null };

function MediaSwitch({
  label,
  description,
  checked,
  onChange,
  ready,
}: {
  label: string;
  description: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  /**
   * False until this modality's model list has answered. The switch's
   * default is read off that list, so a flip before it arrives would act on
   * a guess and then change under the person's hand.
   */
  ready: boolean;
}) {
  const id = useId();
  return (
    <div className="flex items-start justify-between gap-4 px-3 py-2.5">
      <div className="min-w-0">
        <label htmlFor={id} className="text-sm font-medium text-fg">
          {label}
        </label>
        <p id={`${id}-hint`} className="text-xs text-fg-muted">
          {ready ? description : `Checking which ${label.toLowerCase()} models are allowlisted…`}
        </p>
      </div>
      <Switch
        id={id}
        checked={checked}
        onCheckedChange={onChange}
        disabled={!ready}
        aria-describedby={`${id}-hint`}
      />
    </div>
  );
}

/**
 * `ScopePicker` — what this run makes (PRD §15.4 B): which of the frozen
 * plan's campaigns, whether images and video are made at all, and two or
 * three concepts per campaign (§4.3). Every campaign is ticked to start with;
 * a run over none of them is not a run, and the dialog says so.
 */
export function ScopePicker({
  campaigns,
  pending,
  error,
  selected,
  onSelected,
  images,
  onImages,
  video,
  onVideo,
  ready,
  concepts,
  onConcepts,
}: {
  campaigns?: PlanCampaign[];
  pending: boolean;
  error: string | null;
  selected: string[];
  onSelected: (refs: string[]) => void;
  images: boolean;
  onImages: (on: boolean) => void;
  video: boolean;
  onVideo: (on: boolean) => void;
  /** Whether each modality's model list has answered (see `MediaSwitch`). */
  ready: { image: boolean; video: boolean };
  concepts: 2 | 3;
  onConcepts: (concepts: 2 | 3) => void;
}) {
  const legendId = useId();
  const all = campaigns?.map((campaign) => campaign.campaign_ref) ?? [];

  return (
    <div className="flex flex-col gap-5">
      <fieldset className="flex flex-col gap-2" aria-describedby={`${legendId}-count`}>
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <legend id={legendId} className="text-sm font-medium text-fg">
            Campaigns
          </legend>
          {campaigns && campaigns.length > 1 ? (
            <div className="flex items-center gap-1">
              <Button type="button" variant="ghost" size="sm" onClick={() => onSelected(all)}>
                Select all
              </Button>
              <Button type="button" variant="ghost" size="sm" onClick={() => onSelected([])}>
                Clear
              </Button>
            </div>
          ) : null}
        </div>
        {error ? (
          <Alert tone="error" title="The plan's campaigns could not be loaded">
            {error}
          </Alert>
        ) : pending || !campaigns ? (
          <Skeleton className="h-20 w-full" />
        ) : (
          <>
            <ul className="flex flex-col divide-y rounded-token border">
              {campaigns.map((campaign) => {
                const checked = selected.includes(campaign.campaign_ref);
                return (
                  <li key={campaign.campaign_ref}>
                    <label className="flex cursor-pointer items-center gap-3 px-3 py-2 hover:bg-surface-hover">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() =>
                          onSelected(
                            checked
                              ? selected.filter((ref) => ref !== campaign.campaign_ref)
                              : all.filter((ref) => ref === campaign.campaign_ref || selected.includes(ref)),
                          )
                        }
                        className="size-4 shrink-0 accent-accent"
                      />
                      <span className="min-w-0 flex-1 truncate text-sm text-fg">{campaign.name}</span>
                      {campaign.type ? (
                        <span className="shrink-0 rounded-full border px-2 py-0.5 font-mono text-xs text-fg-muted">
                          {campaign.type}
                        </span>
                      ) : null}
                    </label>
                  </li>
                );
              })}
            </ul>
            <p id={`${legendId}-count`} className="text-xs tabular-nums text-fg-muted">
              {selected.length === 0
                ? "No campaign is selected. Choose at least one to start a run."
                : `${selected.length} of ${campaigns.length} campaigns`}
            </p>
          </>
        )}
      </fieldset>

      <div className="flex flex-col divide-y rounded-token border">
        <MediaSwitch
          label="Images"
          description="Concept masters and a rendition for every image ratio the spec sheet requires."
          checked={images}
          onChange={onImages}
          ready={ready.image}
        />
        <MediaSwitch
          label="Video"
          description="A clip for every video ratio the spec sheet requires."
          checked={video}
          onChange={onVideo}
          ready={ready.video}
        />
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <p id={`${legendId}-concepts`} className="text-sm font-medium text-fg">
            Concepts per campaign
          </p>
          <p className="text-xs text-fg-muted">Each concept is a distinct creative angle with its own media.</p>
        </div>
        <SegmentedControl
          label="Concepts per campaign"
          value={String(concepts) as "2" | "3"}
          onChange={(next) => onConcepts(next === "3" ? 3 : 2)}
          options={[
            { value: "2", label: "2" },
            { value: "3", label: "3" },
          ]}
        />
      </div>
    </div>
  );
}
