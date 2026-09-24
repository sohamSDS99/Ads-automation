"use client";

import { TriangleAlert } from "lucide-react";
import { useId } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SegmentedControl } from "@/components/ui/segmented";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import type { CapabilityRefusal, RunDefault } from "@/lib/api/creative";
import { PARAM_LABEL, runParams, type CapabilityRecord, type RunParam } from "@/lib/api/media";

/** The value a segmented control or select uses for "not sent". */
const UNSET = "__default";

/** Up to this many values a choice is a segmented control; past it, a select. */
const SEGMENT_LIMIT = 4;

export type ParamValues = Record<string, RunDefault>;

function describe(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

/** "It accepts 1K, 2K." / "It accepts 1 to 4." / "It does not take `seed` at all." */
function acceptedSentence(refusal: CapabilityRefusal): string {
  const supported = refusal.supported;
  if (Array.isArray(supported)) {
    return supported.length > 0
      ? `It accepts ${supported.map(String).join(", ")}.`
      : `It does not take ${refusal.field} at all.`;
  }
  const { min, max } = supported;
  if (min !== undefined && max !== undefined) return `It accepts ${min} to ${max}.`;
  if (min !== undefined) return `It accepts ${min} or more.`;
  if (max !== undefined) return `It accepts up to ${max}.`;
  return "";
}

/**
 * `CapabilityParams` — the run defaults a chosen model takes (PRD §15.4 B,
 * law 36).
 *
 * **Only the parameters the model supports are drawn.** Which those are is
 * read off its capability record (`runParams`); a parameter it does not
 * support is absent, never disabled, because a greyed-out "quality" still
 * says the model has one. The control follows the descriptor: an enum is a
 * segmented control (a select past four values), a range is a slider with a
 * numeric input, a boolean is a switch.
 *
 * An enum or a range can be left unset — the request then carries nothing
 * and the workspace default, or the model's own, applies server-side. A
 * switch has no third position, so it always sends what it shows; the dialog
 * seeds it when a model is chosen.
 *
 * A `422 capability_unsupported` from the estimate or the start is rendered
 * here: the field, the value refused, and every value the server says the
 * model accepts, each one a click away — the catalogue changed under the
 * choice, and the fix is the server's list, not this component's guess.
 */
export function CapabilityParams({
  record,
  values,
  onChange,
  workspaceDefaults,
  refusal,
}: {
  record: CapabilityRecord;
  values: ParamValues;
  onChange: (field: string, value: RunDefault | undefined) => void;
  /** `media_defaults[modality]`, shown as what "Default" means where it applies. */
  workspaceDefaults: Record<string, unknown>;
  refusal: CapabilityRefusal | null;
}) {
  const params = runParams(record);
  const refusalId = useId();

  return (
    <div className="flex flex-col gap-4">
      {refusal ? (
        <RefusalCallout
          id={refusalId}
          refusal={refusal}
          modelId={record.model_id}
          onUse={(value) => onChange(refusal.field, value)}
        />
      ) : null}

      {params.length === 0 ? (
        <p className="text-sm text-fg-muted">
          This model takes no settings a run can choose; OpenRouter uses its own for each request.
        </p>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2">
          {params.map((param) => (
            <ParamControl
              key={param.field}
              param={param}
              value={values[param.field]}
              workspaceDefault={workspaceDefaults[param.field]}
              invalid={refusal?.field === param.field}
              describedBy={refusal?.field === param.field ? refusalId : undefined}
              onChange={(value) => onChange(param.field, value)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function RefusalCallout({
  id,
  refusal,
  modelId,
  onUse,
}: {
  id: string;
  refusal: CapabilityRefusal;
  modelId: string;
  onUse: (value: RunDefault | undefined) => void;
}) {
  const choices = Array.isArray(refusal.supported) ? refusal.supported : [];
  return (
    <div id={id} role="alert" className="flex flex-col gap-3 rounded-token border border-status-failed/50 p-3">
      <p className="flex items-start gap-2 text-sm text-fg">
        <TriangleAlert className="mt-0.5 size-4 shrink-0 text-status-failed" aria-hidden />
        <span>
          <span className="font-medium">
            {modelId} does not support <span className="font-mono text-xs">{refusal.field}</span>
            {refusal.value !== undefined ? (
              <>
                {" "}
                = <span className="font-mono text-xs">{describe(refusal.value)}</span>
              </>
            ) : null}
            .
          </span>{" "}
          {acceptedSentence(refusal)} Choose one, or leave it to the default.
        </span>
      </p>
      <div className="flex flex-wrap gap-2">
        {choices.map((choice) => (
          <Button
            key={String(choice)}
            type="button"
            variant="secondary"
            size="sm"
            // As the server typed it: a duration is sent back as a number.
            onClick={() => onUse(choice)}
          >
            Use {String(choice)}
          </Button>
        ))}
        <Button type="button" variant="ghost" size="sm" onClick={() => onUse(undefined)}>
          Use the default
        </Button>
      </div>
    </div>
  );
}

function ParamControl({
  param,
  value,
  workspaceDefault,
  invalid,
  describedBy,
  onChange,
}: {
  param: RunParam;
  value: RunDefault | undefined;
  workspaceDefault: unknown;
  invalid: boolean;
  describedBy?: string;
  onChange: (value: RunDefault | undefined) => void;
}) {
  const id = useId();
  const hintId = `${id}-hint`;
  const label = PARAM_LABEL[param.field] ?? param.field;
  const described = [hintId, describedBy].filter(Boolean).join(" ");

  if (param.kind === "boolean") {
    return (
      <div className="flex items-start justify-between gap-3 rounded-token border px-3 py-2.5">
        <div className="min-w-0">
          <label htmlFor={id} className="text-sm font-medium text-fg">
            {label}
          </label>
          <p id={hintId} className="text-xs text-fg-muted">
            Priced {value === true ? "with" : "without"} it.
          </p>
        </div>
        <Switch
          id={id}
          checked={value === true}
          onCheckedChange={(checked) => onChange(checked)}
          aria-describedby={described}
        />
      </div>
    );
  }

  // What "Default" means here: the workspace's, when it is one this control
  // could show; otherwise the model's own. The server applies it either way.
  const shownDefault =
    workspaceDefault === undefined || workspaceDefault === null
      ? null
      : param.kind === "enum"
        ? param.values.includes(String(workspaceDefault))
          ? String(workspaceDefault)
          : null
        : typeof workspaceDefault === "number"
          ? String(workspaceDefault)
          : null;
  const hint = shownDefault ? `Default: ${shownDefault}, the workspace setting` : "Default: the model's own";

  if (param.kind === "enum") {
    const selected = value === undefined ? UNSET : String(value);
    const pick = (next: string) =>
      onChange(next === UNSET ? undefined : param.numeric ? Number(next) : next);
    const unit = param.field === "duration" ? " s" : "";
    return (
      <div className="flex min-w-0 flex-col gap-1.5">
        <span id={`${id}-label`} className="text-sm font-medium text-fg">
          {label}
        </span>
        {param.values.length <= SEGMENT_LIMIT ? (
          <SegmentedControl
            label={label}
            describedBy={described}
            value={param.values.includes(selected) || selected === UNSET ? selected : null}
            onChange={pick}
            options={[
              { value: UNSET, label: "Default", hint },
              ...param.values.map((option) => ({ value: option, label: `${option}${unit}` })),
            ]}
          />
        ) : (
          <Select
            aria-labelledby={`${id}-label`}
            aria-describedby={described}
            aria-invalid={invalid || undefined}
            invalid={invalid}
            value={param.values.includes(selected) ? selected : UNSET}
            onChange={(event) => pick(event.target.value)}
          >
            <option value={UNSET}>Default</option>
            {param.values.map((option) => (
              <option key={option} value={option}>
                {option}
                {unit}
              </option>
            ))}
          </Select>
        )}
        <p id={hintId} className="text-xs text-fg-muted">
          {hint}
        </p>
      </div>
    );
  }

  // A range: a slider for the gesture, a number for the exact value. Empty
  // number = unset. Without both bounds there is no track to slide along.
  const current = typeof value === "number" ? value : null;
  const bounded = param.min !== null && param.max !== null;
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium text-fg">
        {label}
      </label>
      <div className="flex items-center gap-3">
        {bounded ? (
          <input
            type="range"
            aria-label={`${label} slider`}
            aria-describedby={described}
            min={param.min ?? undefined}
            max={param.max ?? undefined}
            step={1}
            value={current ?? param.min ?? 0}
            onChange={(event) => onChange(Number(event.target.value))}
            className="min-w-0 flex-1 accent-accent"
          />
        ) : null}
        <Input
          id={id}
          type="number"
          inputMode="numeric"
          min={param.min ?? undefined}
          max={param.max ?? undefined}
          step={1}
          placeholder="Default"
          aria-describedby={described}
          aria-invalid={invalid || undefined}
          value={current ?? ""}
          onChange={(event) => {
            const raw = event.target.value;
            onChange(raw === "" ? undefined : Math.trunc(Number(raw)));
          }}
          className="w-24 tabular-nums"
        />
      </div>
      <p id={hintId} className="text-xs text-fg-muted">
        {bounded ? `${param.min} to ${param.max}. ` : ""}
        {current === null ? hint : "Clear the number to use the default."}
      </p>
    </div>
  );
}
