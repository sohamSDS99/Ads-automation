"use client";

import { RefreshCw } from "lucide-react";
import { useEffect, useId, useMemo, useState } from "react";

import { CapabilityParams, type ParamValues } from "@/components/creative/capability-params";
import { ModelPicker, modelKey } from "@/components/creative/model-picker";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import { capabilityRefusal, type RunDefault } from "@/lib/api/creative";
import type { MediaModality, MediaModelRow } from "@/lib/api/media";
import type { RegenerationRequest } from "@/lib/api/media-library";
import { usd } from "@/lib/format";
import { errorMessage, useMediaModels, useMediaSettings, useRegenerateAsset, useRegenerationEstimate } from "@/lib/queries";

/** The lint preview's debounce (§15.4 E): a price per keystroke is a price per pause. */
const DEBOUNCE_MS = 250;
const NOTE_MAX = 500;

/**
 * `GenerationPanel` (Stage 04 PRD §15.4 G): regenerate one image or video
 * with a note, optionally on another allowlisted model through the same
 * `ModelPicker` the Start dialog uses, with only the parameters that model
 * supports (`CapabilityParams`) — and, before anything is sent, what it costs
 * against what remains of the media cap (§15.2 rule 8):
 * "This costs ≈ $0.04 · $38.12 of $40.00 remains".
 *
 * The price is the server's (`regeneration-estimate`), for exactly the model
 * and parameters shown; so is the refusal of an unsupported one (law 36).
 * The run's model stays selected unless the person picks another — nothing
 * is swapped for them. Rendered only for `creative_execute`.
 */
export function GenerationPanel({
  runId,
  projectId,
  assetId,
  modality,
  current,
}: {
  runId: string;
  projectId: string;
  assetId: string;
  modality: MediaModality;
  /** The model that made this asset, from its newest job. */
  current: { model_id: string; provider_tag: string | null } | null;
}) {
  const noteId = useId();
  const pickerId = useId();
  const [note, setNote] = useState("");
  const [picked, setPicked] = useState<MediaModelRow | null>(null);
  const [values, setValues] = useState<ParamValues>({});
  const models = useMediaModels(modality, projectId, true);
  const settings = useMediaSettings(true);
  const regenerate = useRegenerateAsset(runId);

  const runRow = useMemo(
    () =>
      current
        ? (models.data?.models.find((row) => modelKey(row) === modelKey(current)) ??
          models.data?.models.find((row) => row.model_id === current.model_id) ??
          null)
        : null,
    [models.data, current],
  );
  const chosen = picked ?? runRow;
  const switched = Boolean(picked && current && picked.model_id !== current.model_id);

  const request = useMemo<RegenerationRequest>(
    () => ({
      ...(switched && picked ? { model_override: picked.model_id, provider_tag: picked.provider_tag } : {}),
      params_override: values as Record<string, string | number | boolean>,
    }),
    [switched, picked, values],
  );
  const [debounced, setDebounced] = useState<RegenerationRequest>(request);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(request), DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [request]);
  const settled = JSON.stringify(debounced) === JSON.stringify(request);

  const estimate = useRegenerationEstimate(assetId, debounced);
  const refusal = estimate.isError ? capabilityRefusal(estimate.error) : null;
  const price = estimate.data;
  const noun = modality === "image" ? "image" : "video";
  const ready = Boolean(note.trim()) && Boolean(price?.fits) && settled && !estimate.isFetching && !regenerate.isPending;

  const onParam = (field: string, value: RunDefault | undefined) =>
    setValues((now) => {
      const next = { ...now };
      if (value === undefined) delete next[field];
      else next[field] = value;
      return next;
    });

  const submit = () => {
    regenerate.mutate(
      { assetId, note: note.trim(), ...debounced },
      {
        onSuccess: (accepted) => {
          toast.success(`Regeneration queued as job ${accepted.job_id.slice(0, 4)}…${accepted.job_id.slice(-4)}`);
          setNote("");
        },
        onError: (error) =>
          toast.error(
            error instanceof ApiError
              ? (error.problem?.detail ?? `The regeneration was refused (${error.status}).`)
              : "The regeneration could not be sent. Check the connection and try again.",
          ),
      },
    );
  };

  return (
    <section className="flex flex-col gap-4" aria-labelledby={`${noteId}-title`} data-testid="generation-panel">
      <div className="flex flex-col gap-1">
        <h3 id={`${noteId}-title`} className="text-sm font-semibold text-fg">
          Regenerate this {noun}
        </h3>
        <p className="text-xs text-fg-muted">
          A new {noun} is requested with your note, before review. The model paints pixels only: logos,
          captions, the end card and disclosure labels are still placed by code.
        </p>
      </div>

      <label htmlFor={noteId} className="flex flex-col gap-1.5 text-sm">
        <span className="flex items-baseline justify-between">
          <span className="font-medium text-fg">Note for the model</span>
          <span className="text-xs tabular-nums text-fg-muted">
            {note.length}/{NOTE_MAX}
          </span>
        </span>
        <Textarea
          id={noteId}
          value={note}
          maxLength={NOTE_MAX}
          rows={3}
          onChange={(event) => setNote(event.target.value)}
          placeholder="What should change: the setting, the framing, what to leave out"
        />
      </label>

      <div className="flex flex-col gap-1.5">
        <label htmlFor={pickerId} className="text-sm font-medium text-fg">
          Model
        </label>
        <ModelPicker
          modality={modality}
          id={pickerId}
          models={models.data}
          pending={models.isPending}
          error={errorMessage(models)}
          value={chosen ? modelKey(chosen) : null}
          onChange={(row) => {
            setPicked(current && modelKey(row) === modelKey(current) ? null : row);
            setValues({});
          }}
        />
        <p className="text-xs text-fg-muted">
          {switched
            ? `Switching from ${current?.model_id}: the new model starts from the workspace defaults.`
            : "The model this run used. Choose another allowlisted model to switch; nothing is switched for you."}
        </p>
      </div>

      {chosen?.capability ? (
        <CapabilityParams
          record={chosen.capability}
          values={values}
          onChange={onParam}
          workspaceDefaults={settings.data?.media_defaults[modality] ?? {}}
          refusal={refusal}
        />
      ) : null}

      <div className="flex flex-col gap-2 border-t pt-3" aria-live="polite" data-testid="regeneration-cost">
        {price ? (
          <p className="text-sm tabular-nums text-fg" data-fits={price.fits ? "true" : "false"}>
            This costs ≈ <span className="font-semibold">{usd(price.estimate_usd)}</span> ·{" "}
            <span className="font-semibold">{usd(price.remaining_after_usd)}</span> of {usd(price.media.cap_usd)}{" "}
            remains
            <span className="block text-xs text-fg-muted">
              {price.requests === 1 ? "One request" : `${price.requests} requests, one per planned clip`} on{" "}
              <span className="font-mono">{price.model_id}</span> · {price.confidence} confidence ·{" "}
              {usd(price.media.spent_usd)} spent and {usd(price.media.reserved_usd)} reserved so far
              {estimate.isFetching || !settled ? " · updating" : ""}
            </span>
          </p>
        ) : estimate.isPending && !estimate.isError ? (
          <p className="text-sm text-fg-muted">Pricing this regeneration against the media budget…</p>
        ) : null}
        {price && !price.fits ? (
          <Alert tone="warning" title="Over the media budget">
            Regenerating needs ≈ {usd(price.estimate_usd)}, but {usd(price.remaining_usd)} of the{" "}
            {usd(price.media.cap_usd)} media cap remains. Choose a cheaper model or lower the resolution.
          </Alert>
        ) : null}
        {estimate.isError && !refusal ? (
          <Alert tone="error" title="This regeneration cannot be priced">
            {errorMessage(estimate)}
          </Alert>
        ) : null}
        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={submit} disabled={!ready}>
            <RefreshCw className="size-4" aria-hidden />
            Regenerate {noun}
            {price ? ` · ≈ ${usd(price.estimate_usd)}` : ""}
          </Button>
          {!note.trim() ? <span className="text-xs text-fg-muted">Write a note for the model first.</span> : null}
        </div>
      </div>
    </section>
  );
}
