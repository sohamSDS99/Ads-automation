"use client";

import { useQueryClient } from "@tanstack/react-query";
import { TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useEffect, useId, useMemo, useState, type ReactNode } from "react";

import { CapabilityParams, type ParamValues } from "@/components/creative/capability-params";
import { CostEstimate } from "@/components/creative/cost-estimate";
import { ModelPicker, modelKey } from "@/components/creative/model-picker";
import { PinSummary } from "@/components/creative/pin-summary";
import { RatioCoverageTable } from "@/components/creative/ratio-coverage";
import { ScopePicker } from "@/components/creative/scope-picker";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTrigger } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import {
  capabilityRefusal,
  destinationLabel,
  reductionLabel,
  type CreativeEligibility,
  type CreativeRequest,
  type MediaModelSelection,
  type RunDefault,
} from "@/lib/api/creative";
import { runParams, type MediaModality, type MediaModelRow } from "@/lib/api/media";
import { usd } from "@/lib/format";
import {
  errorMessage,
  keys,
  useCreativeEstimate,
  useMediaModels,
  useMediaSettings,
  usePlanCampaigns,
  useStartCreativeRun,
} from "@/lib/queries";

/** How long the scope must sit still before it is priced (§15.2: debounced, never per keystroke). */
const SETTLE_MS = 250;

const MODALITIES: MediaModality[] = ["image", "video"];

/**
 * `StartCreativeDialog` — where a person chooses what a creative run makes
 * and which models make it, and sees what it will cost before it spends
 * anything (PRD §15.4 B, §9.2–9.3, law 36).
 *
 * A full-height sheet in three sections — *Pins*, *Scope*, *Models* — then
 * the two consequences of those choices: the ratio-coverage table and the
 * `CostEstimate`. The one primary button states the spend (`Start run · est.
 * $31.20`).
 *
 * Nothing here decides anything. The scope is priced by `POST /estimate` for
 * exactly what is on screen; the button is enabled only while that answer is
 * current and says `fits`, and over a cap it offers the server's
 * `scope_reduction` as one click. The start is re-checked server-side in
 * full; a `409` renders its blockers and a `422` names the field.
 */
export function StartCreativeDialog({
  projectId,
  pins,
}: {
  projectId: string;
  pins: CreativeEligibility["pins"];
}) {
  const [open, setOpen] = useState(false);
  // A started run clears the form; closing it without starting keeps it.
  const [generation, setGeneration] = useState(0);
  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        <Button className="self-start">Choose scope and models</Button>
      </SheetTrigger>
      <StartSheet
        key={generation}
        projectId={projectId}
        pins={pins}
        open={open}
        onStarted={() => {
          setOpen(false);
          setGeneration((value) => value + 1);
        }}
      />
    </Sheet>
  );
}

function Section({ title, children, id }: { title: string; children: ReactNode; id?: string }) {
  return (
    <section aria-labelledby={id} className="flex flex-col gap-3 border-b px-4 py-5 last:border-b-0 sm:px-6">
      <h3 id={id} className="text-sm font-semibold text-fg">
        {title}
      </h3>
      {children}
    </section>
  );
}

function StartSheet({
  projectId,
  pins,
  open,
  onStarted,
}: {
  projectId: string;
  pins: CreativeEligibility["pins"];
  open: boolean;
  onStarted: () => void;
}) {
  const client = useQueryClient();
  const ids = useId();
  const planRun = typeof pins.plan_run_id === "string" ? pins.plan_run_id : null;

  const campaigns = usePlanCampaigns(planRun, open);
  const models = {
    image: useMediaModels("image", projectId, open),
    video: useMediaModels("video", projectId, open),
  };
  const settings = useMediaSettings(open);

  // Scope. `null` = not touched yet, so the default is derived from the data
  // (every campaign; a modality on when it has a model to choose) instead of
  // being copied into state by an effect.
  const [picked, setPicked] = useState<string[] | null>(null);
  const [switches, setSwitches] = useState<Record<MediaModality, boolean | null>>({ image: null, video: null });
  const [concepts, setConcepts] = useState<2 | 3>(2);
  const [chosen, setChosen] = useState<Record<MediaModality, string | null>>({ image: null, video: null });
  const [params, setParams] = useState<Record<MediaModality, ParamValues>>({ image: {}, video: {} });

  const allRefs = useMemo(() => campaigns.data?.map((campaign) => campaign.campaign_ref) ?? [], [campaigns.data]);
  const refs = picked ?? allRefs;
  const on = (modality: MediaModality): boolean =>
    switches[modality] ?? Boolean(models[modality].data?.models.some((row) => row.available));
  const rowFor = (modality: MediaModality): MediaModelRow | undefined =>
    models[modality].data?.models.find((row) => row.available && modelKey(row) === chosen[modality]);

  const choose = (modality: MediaModality, row: MediaModelRow) => {
    setChosen((current) => ({ ...current, [modality]: modelKey(row) }));
    // A switch has no "unset", so it starts at the workspace default when
    // there is one and off otherwise — and then always sends what it shows.
    const seeded: ParamValues = {};
    const defaults = settings.data?.media_defaults[modality] ?? {};
    for (const param of row.capability ? runParams(row.capability) : []) {
      if (param.kind === "boolean") {
        const inherited = defaults[param.field];
        seeded[param.field] = typeof inherited === "boolean" ? inherited : false;
      }
    }
    setParams((current) => ({ ...current, [modality]: seeded }));
  };

  const setParam = (modality: MediaModality, field: string, value: RunDefault | undefined) =>
    setParams((current) => {
      const next = { ...current[modality] };
      if (value === undefined) delete next[field];
      else next[field] = value;
      return { ...current, [modality]: next };
    });

  // The request on screen, or why there is none yet.
  let missing: string | null = null;
  let request: CreativeRequest | null = null;
  if (!campaigns.data) missing = "Loading the plan's campaigns…";
  else if (refs.length === 0) missing = "Choose at least one campaign";
  else {
    const media: MediaModelSelection[] = [];
    for (const modality of MODALITIES) {
      if (!on(modality)) continue;
      const row = rowFor(modality);
      if (!row) {
        missing = modality === "image" ? "Choose an image model" : "Choose a video model";
        break;
      }
      media.push({
        modality,
        model_id: row.model_id,
        provider_tag: row.provider_tag,
        defaults: params[modality],
      });
    }
    if (!missing) {
      request = {
        scope: { campaign_refs: refs, images: on("image"), video: on("video"), concepts_per_campaign: concepts },
        media_models: media,
      };
    }
  }

  // Priced once the request has sat still for SETTLE_MS.
  const requestKey = request ? JSON.stringify(request) : "";
  const [settledKey, setSettledKey] = useState(requestKey);
  useEffect(() => {
    const timer = setTimeout(() => setSettledKey(requestKey), SETTLE_MS);
    return () => clearTimeout(timer);
  }, [requestKey]);
  const settled = settledKey ? (JSON.parse(settledKey) as CreativeRequest) : null;
  const estimate = useCreativeEstimate(projectId, open ? settled : null);

  const start = useStartCreativeRun(projectId);
  const [startedWith, setStartedWith] = useState<string | null>(null);
  const startError = startedWith === requestKey ? start.error : null;

  const refusal = capabilityRefusal(startError) ?? capabilityRefusal(estimate.error);
  // The catalogue moved under the choice: read the model list again so the
  // rows, glyphs and controls catch up with what the server validated against.
  const refusedModality = refusal?.modality ?? null;
  useEffect(() => {
    if (refusedModality) {
      void client.invalidateQueries({ queryKey: keys.mediaModels(refusedModality, projectId) });
    }
  }, [client, projectId, refusedModality]);

  const current =
    request !== null && settledKey === requestKey && !estimate.isPlaceholderData && !estimate.isFetching;
  const answer = current && !estimate.error ? estimate.data : undefined;
  const reduction = answer && !answer.fits ? answer.scope_reduction : null;
  const blockers = startError instanceof ApiError && startError.status === 409 ? (startError.problem?.blockers ?? []) : [];
  const otherError =
    !refusal && blockers.length === 0
      ? startError instanceof ApiError
        ? startError.detail
        : estimate.error instanceof ApiError
          ? estimate.error.detail
          : estimate.error
            ? "The estimate could not be computed."
            : null
      : null;

  let label: string;
  if (start.isPending) label = "Starting run…";
  else if (missing) label = missing;
  else if (answer) label = `Start run · est. ${usd(answer.total_usd)}`;
  else if (estimate.error) label = "Start run";
  else label = estimate.data ? `Start run · est. ${usd(estimate.data.total_usd)} · updating` : "Start run · estimating…";
  const canStart = Boolean(answer?.fits) && !start.isPending && request !== null;

  const submit = () => {
    if (!request || !canStart) return;
    setStartedWith(requestKey);
    start.mutate(request, {
      onSuccess: (accepted) => {
        toast.success(`Creative run ${accepted.run_id.slice(0, 8)} started. It is ${accepted.status}.`);
        onStarted();
      },
    });
  };

  const applyReduction = () => {
    if (!reduction) return;
    setConcepts(reduction.scope.concepts_per_campaign);
    setSwitches({ image: reduction.scope.images, video: reduction.scope.video });
  };

  const footer = (
    <div className="flex flex-col gap-3">
      {answer && !answer.fits ? (
        <div className="flex flex-col gap-2 text-sm" role="status">
          <p className="flex items-start gap-2 text-fg">
            <TriangleAlert className="mt-0.5 size-4 shrink-0 text-status-failed" aria-hidden />
            <span>
              {`Estimated ${usd(answer.total_usd)} is over a cap (${usd(answer.caps.max_creative_cost_usd)} total, ${usd(answer.caps.max_media_cost_usd)} media).`}{" "}
              {reduction?.fits
                ? `${reductionLabel(reduction.steps)} to bring it to ${usd(reduction.total_usd)}.`
                : reduction
                  ? `Even with ${reductionLabel(reduction.steps).replace(/^Use /, "")} it is ${usd(reduction.total_usd)}.`
                  : null}
            </span>
          </p>
          {!reduction?.fits ? (
            <p className="text-fg-muted">
              No change the scope can make fits both caps. Narrow the campaigns, turn images off, or ask an
              admin to raise a cap in{" "}
              <Link href="/settings/models" className="font-medium text-fg underline underline-offset-2">
                model settings
              </Link>
              .
            </p>
          ) : null}
        </div>
      ) : null}
      <div className="flex flex-col-reverse gap-2 sm:flex-row sm:items-center sm:justify-end">
        {reduction?.fits ? (
          <Button type="button" variant="secondary" onClick={applyReduction}>
            {`${reductionLabel(reduction.steps)} · est. ${usd(reduction.total_usd)}`}
          </Button>
        ) : null}
        <Button type="button" onClick={submit} disabled={!canStart} aria-describedby={`${ids}-spend`}>
          <span className="tabular-nums">{label}</span>
        </Button>
      </div>
      <p id={`${ids}-spend`} className="sr-only">
        {answer ? `Starting spends up to about ${usd(answer.total_usd)} and reserves it against the caps.` : ""}
      </p>
    </div>
  );

  return (
    <SheetContent
      title="Start a creative run"
      description="Choose what this run makes and which models make it. Nothing is spent until you start it."
      footer={footer}
    >
      <Section title="Pins" id={`${ids}-pins`}>
        <PinSummary projectId={projectId} pins={pins} />
      </Section>

      <Section title="Scope" id={`${ids}-scope`}>
        <ScopePicker
          campaigns={campaigns.data}
          pending={campaigns.isPending}
          error={errorMessage(campaigns)}
          selected={refs}
          onSelected={setPicked}
          images={on("image")}
          onImages={(value) => setSwitches((current) => ({ ...current, image: value }))}
          video={on("video")}
          onVideo={(value) => setSwitches((current) => ({ ...current, video: value }))}
          concepts={concepts}
          onConcepts={setConcepts}
        />
      </Section>

      <Section title="Models" id={`${ids}-models`}>
        {!on("image") && !on("video") ? (
          <p className="text-sm text-fg-muted">
            Images and video are off, so this run writes text only and needs no media model.
          </p>
        ) : null}
        <div className="flex flex-col gap-6">
          {MODALITIES.filter(on).map((modality) => {
            const row = rowFor(modality);
            const pickerId = `${ids}-${modality}-model`;
            return (
              <div key={modality} className="flex flex-col gap-3">
                <label htmlFor={pickerId} className="text-sm font-medium text-fg">
                  {modality === "image" ? "Image model" : "Video model"}
                </label>
                <ModelPicker
                  modality={modality}
                  id={pickerId}
                  models={models[modality].data}
                  pending={models[modality].isPending}
                  error={errorMessage(models[modality])}
                  value={chosen[modality]}
                  onChange={(next) => choose(modality, next)}
                />
                {row?.capability ? (
                  <CapabilityParams
                    record={row.capability}
                    values={params[modality]}
                    onChange={(field, value) => setParam(modality, field, value)}
                    workspaceDefaults={settings.data?.media_defaults[modality] ?? {}}
                    refusal={refusal && (refusal.modality === modality || refusal.modality === null) ? refusal : null}
                  />
                ) : null}
              </div>
            );
          })}
        </div>
      </Section>

      {on("image") || on("video") ? (
        <Section title="Ratio coverage" id={`${ids}-ratios`}>
          {estimate.data ? (
            <RatioCoverageTable plan={estimate.data.ratio_plan} />
          ) : missing ? (
            <p className="text-sm text-fg-muted">Choose the models to see how each required ratio gets made.</p>
          ) : (
            <Skeleton className="h-24 w-full" />
          )}
        </Section>
      ) : null}

      <Section title="Estimated spend" id={`${ids}-spend-section`}>
        {blockers.length > 0 ? (
          <div className="flex flex-col gap-2" role="alert">
            <p className="text-sm font-medium text-fg">
              {blockers.length === 1
                ? "The run could not start. One thing has to change first:"
                : `The run could not start. ${blockers.length} things have to change first:`}
            </p>
            <ul className="flex flex-col divide-y rounded-token border">
              {blockers.map((blocker) => (
                <li key={blocker.code} className="flex flex-col gap-1 px-3 py-2 text-sm">
                  <span className="text-fg">{blocker.detail}</span>
                  <Link
                    href={blocker.fix_url}
                    className="self-start text-xs font-medium text-fg underline underline-offset-2"
                  >
                    {destinationLabel(blocker.fix_url)}
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {otherError ? (
          <Alert tone="error" title="This scope cannot be priced or started">
            {otherError}
          </Alert>
        ) : null}
        {missing ? (
          <p className="text-sm text-fg-muted">{missing} to see what this run is estimated to spend.</p>
        ) : refusal ? (
          <p className="text-sm text-fg-muted">
            Nothing is priced while a model setting is refused — fix it under Models above.
          </p>
        ) : estimate.error ? null : (
          <CostEstimate estimate={estimate.data} updating={!current} />
        )}
      </Section>
    </SheetContent>
  );
}
