"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Film, Image as ImageIcon, type LucideIcon } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import {
  EMPTY_ALLOWLIST,
  capabilityChips,
  priceLabel,
  putMediaSettings,
  type CapabilityRecord,
  type MediaAllowlist,
  type MediaAllowlistEntry,
  type MediaCatalogue,
  type MediaModality,
} from "@/lib/api/media";
import { keys, useMediaCatalogue, useMediaSettings } from "@/lib/queries";

/**
 * `/settings/models` → Media generation: the two allowlists (PRD §9.2).
 *
 * An admin picks, from the live catalogue, which image and which video models
 * an operator may choose when starting a run, and optionally pins a provider
 * for each. Nothing is allowed until somebody allows it — there is no seed
 * default for IMAGE_GEN or VIDEO_GEN (law 36) — so the empty state is the
 * first state, and it says what it stops.
 *
 * Tables, because a catalogue is data (§15.2 rule 4); one card around them,
 * because saving the list is a decision. Before the save, the footer states
 * what the save does in numbers (§15.2 rule 8).
 *
 * Mounted for `settings_write` holders only — the catalogue route refuses
 * anyone else, and a control that can only fail is absent, not disabled.
 */
export function MediaAllowlistEditor() {
  const queryClient = useQueryClient();
  const settings = useMediaSettings(true);
  const image = useMediaCatalogue("image", true);
  const video = useMediaCatalogue("video", true);

  const saved = settings.data?.media_allowlist ?? EMPTY_ALLOWLIST;
  // `null` until somebody changes something, so a refetch of the saved list
  // is shown as-is instead of being shadowed by a stale copy of itself.
  const [draft, setDraft] = useState<MediaAllowlist | null>(null);
  const current = draft ?? saved;
  const dirty = draft !== null && canonical(draft) !== canonical(saved);

  const save = useMutation({
    mutationFn: () => putMediaSettings({ media_allowlist: current }),
    onSuccess: (result) => {
      queryClient.setQueryData(keys.mediaSettings, result);
      setDraft(null);
      toast.success("Media allowlist saved");
    },
    onError: (error) =>
      toast.error("Allowlist not saved", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      }),
  });

  const allowed = {
    image: current.image.filter((entry) => entry.enabled).length,
    video: current.video.filter((entry) => entry.enabled).length,
  };

  function update(modality: MediaModality, next: MediaAllowlistEntry[]) {
    setDraft({ ...current, [modality]: next });
  }

  function toggle(modality: MediaModality, modelId: string, on: boolean) {
    const entries = current[modality];
    const existing = entries.find((entry) => entry.model_id === modelId);
    const wasSaved = saved[modality].some((entry) => entry.model_id === modelId);
    if (on) {
      update(
        modality,
        existing
          ? entries.map((entry) => (entry.model_id === modelId ? { ...entry, enabled: true } : entry))
          : [...entries, { model_id: modelId, provider_tag: null, enabled: true }],
      );
    } else if (wasSaved) {
      // A saved entry is switched off rather than deleted, so its provider pin
      // survives being turned off and on again.
      update(
        modality,
        entries.map((entry) => (entry.model_id === modelId ? { ...entry, enabled: false } : entry)),
      );
    } else {
      update(
        modality,
        entries.filter((entry) => entry.model_id !== modelId),
      );
    }
  }

  function pin(modality: MediaModality, modelId: string, providerTag: string | null) {
    update(
      modality,
      current[modality].map((entry) =>
        entry.model_id === modelId ? { ...entry, provider_tag: providerTag } : entry,
      ),
    );
  }

  const loading = settings.isPending;

  return (
    <Card>
      <CardHeader
        title="Media generation"
        description="Which image and video models an operator may choose when they start a creative run. Nothing is allowed until you allow it."
      />
      <CardBody className="flex flex-col gap-8">
        {settings.isError ? (
          <Alert tone="error" title="The media allowlist could not be read">
            {settings.error instanceof ApiError ? settings.error.detail : "Try again in a moment."}
          </Alert>
        ) : null}

        <ModalityTable
          modality="image"
          title="Image models"
          icon={ImageIcon}
          catalogue={image}
          entries={loading ? undefined : current.image}
          onToggle={(id, on) => toggle("image", id, on)}
          onPin={(id, tag) => pin("image", id, tag)}
        />
        <ModalityTable
          modality="video"
          title="Video models"
          icon={Film}
          catalogue={video}
          entries={loading ? undefined : current.video}
          onToggle={(id, on) => toggle("video", id, on)}
          onPin={(id, tag) => pin("video", id, tag)}
        />
      </CardBody>
      <CardFooter>
        {dirty ? (
          <div className="flex w-full flex-wrap items-center justify-end gap-4">
            <p className="mr-auto max-w-prose text-sm text-fg-muted">
              {consequence(allowed.image, allowed.video)}
            </p>
            <Button variant="ghost" onClick={() => setDraft(null)} disabled={save.isPending}>
              Discard changes
            </Button>
            <Button onClick={() => save.mutate()} disabled={save.isPending}>
              {save.isPending ? <Spinner label="Saving" /> : null}
              Save allowlist
            </Button>
          </div>
        ) : (
          <p className="text-sm text-fg-subtle">
            {loading ? "Reading the allowlist" : `${summary(allowed.image, allowed.video)} No unsaved changes.`}
          </p>
        )}
      </CardFooter>
    </Card>
  );
}

/** Stable text for comparing two allowlists, whatever order they were built in. */
function canonical(list: MediaAllowlist): string {
  const sorted = (entries: MediaAllowlistEntry[]) =>
    [...entries]
      .map((entry) => ({ model_id: entry.model_id, provider_tag: entry.provider_tag ?? null, enabled: entry.enabled }))
      .sort((a, b) => a.model_id.localeCompare(b.model_id));
  return JSON.stringify({ image: sorted(list.image), video: sorted(list.video) });
}

function count(n: number, modality: MediaModality): string {
  return `${n} ${modality} ${n === 1 ? "model" : "models"}`;
}

function summary(image: number, video: number): string {
  return `${count(image, "image")} and ${count(video, "video")} allowed.`;
}

/**
 * What saving does, before it happens. The zero cases name what they stop,
 * because an allowlist emptied by accident is a stage nobody can start.
 */
function consequence(image: number, video: number): string {
  const stops = [
    image === 0 ? "a run with images cannot start" : null,
    video === 0 ? "a run with video cannot start" : null,
  ].filter(Boolean);
  const lead = `Saving allows ${count(image, "image")} and ${count(video, "video")}.`;
  const tail = "Runs already started keep the model they pinned.";
  return stops.length > 0 ? `${lead} Until one is allowed, ${stops.join(" and ")}. ${tail}` : `${lead} ${tail}`;
}

/** One model, however many provider endpoints the catalogue lists for it. */
type ModelRow = { modelId: string; record: CapabilityRecord | null; providers: string[] };

function rowsFor(catalogue: MediaCatalogue | undefined, entries: MediaAllowlistEntry[]): ModelRow[] {
  const byId = new Map<string, ModelRow>();
  for (const record of catalogue?.models ?? []) {
    const row = byId.get(record.model_id) ?? { modelId: record.model_id, record, providers: [] };
    if (record.provider_tag && !row.providers.includes(record.provider_tag)) {
      row.providers.push(record.provider_tag);
    }
    byId.set(record.model_id, row);
  }
  // A model on the list that the catalogue no longer carries is still shown,
  // so it can be taken off: a run cannot use it (CR-E8), and an allowlist
  // that hides its own dead entries is one nobody can clean up.
  if (catalogue) {
    for (const entry of entries) {
      if (!byId.has(entry.model_id)) byId.set(entry.model_id, { modelId: entry.model_id, record: null, providers: [] });
    }
  }
  return [...byId.values()].sort((a, b) => a.modelId.localeCompare(b.modelId));
}

function ModalityTable({
  modality,
  title,
  icon: Icon,
  catalogue,
  entries,
  onToggle,
  onPin,
}: {
  modality: MediaModality;
  title: string;
  icon: LucideIcon;
  catalogue: ReturnType<typeof useMediaCatalogue>;
  entries: MediaAllowlistEntry[] | undefined;
  onToggle: (modelId: string, on: boolean) => void;
  onPin: (modelId: string, providerTag: string | null) => void;
}) {
  const rows = useMemo(() => rowsFor(catalogue.data, entries ?? []), [catalogue.data, entries]);
  const enabled = (entries ?? []).filter((entry) => entry.enabled).length;
  const headingId = `media-${modality}`;
  const noKey = catalogue.error instanceof ApiError && catalogue.error.status === 409;

  return (
    <section aria-labelledby={headingId} className="flex min-w-0 flex-col gap-2">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 id={headingId} className="flex items-center gap-2 text-sm font-medium text-fg">
          <Icon className="size-4 text-fg-subtle" aria-hidden />
          {title}
        </h3>
        {entries ? (
          <span className="text-sm tabular-nums text-fg-muted">
            {enabled} of {rows.length} allowed
          </span>
        ) : null}
      </div>

      {catalogue.data?.warning ? (
        <Alert tone="warning" title="Showing the last catalogue that loaded">
          {catalogue.data.warning}
        </Alert>
      ) : null}

      {noKey ? (
        <Alert tone="warning" title={`Add the OpenRouter key to see ${modality} models`}>
          The media catalogue comes from OpenRouter, so it cannot be read until a key is stored.{" "}
          <Link href="/settings/connections" className="text-accent hover:underline">
            Connect OpenRouter
          </Link>
          .
        </Alert>
      ) : catalogue.isError ? (
        <Alert tone="error" title={`The ${modality} catalogue could not be read`}>
          {catalogue.error instanceof ApiError ? catalogue.error.detail : "Try again in a moment."}{" "}
          Saved choices are kept; they cannot be changed until it loads.
        </Alert>
      ) : catalogue.isPending || !entries ? (
        <div className="flex flex-col gap-2 rounded-token border p-4">
          <Skeleton className="h-5 w-full" />
          <Skeleton className="h-5 w-full" />
          <Skeleton className="h-5 w-2/3" />
        </div>
      ) : rows.length === 0 ? (
        <p className="rounded-token border px-4 py-4 text-sm text-fg-muted">
          OpenRouter lists no {modality} models right now. Nothing can be allowed until it does.
        </p>
      ) : (
        <div className="min-w-0 rounded-token border">
          {/* `min-w-0` undoes `Table`'s `min-w-max`, so a long model id wraps
              inside its cell instead of widening the table past a phone. */}
          <Table label={`${title} in the live catalogue`} className="min-w-0">
            <thead>
              <Tr>
                <Th className="w-0">Allow</Th>
                <Th>Model</Th>
                <Th className="hidden sm:table-cell">Price</Th>
                <Th className="hidden sm:table-cell">Provider</Th>
              </Tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const entry = entries.find((item) => item.model_id === row.modelId);
                const on = entry?.enabled ?? false;
                return (
                  <Tr key={row.modelId}>
                    <Td>
                      <label className="flex items-center">
                        <input
                          type="checkbox"
                          checked={on}
                          onChange={(event) => onToggle(row.modelId, event.target.checked)}
                          className="size-4 accent-accent"
                        />
                        <span className="sr-only">Allow {row.modelId}</span>
                      </label>
                    </Td>
                    <Td>
                      {/* `break-words`, not `break-all`: an id wraps at its own
                          `/` and `-`, so a phone reads `black-forest-labs/`
                          and not `black-fo` `rest-lab`. */}
                      <p className="break-words font-mono text-xs text-fg">{row.modelId}</p>
                      {row.record ? (
                        <p className="mt-1 flex flex-wrap gap-1">
                          {capabilityChips(row.record).map((chip) => (
                            <Badge key={chip}>
                              <span className="tabular-nums">{chip}</span>
                            </Badge>
                          ))}
                        </p>
                      ) : (
                        <p className="mt-1">
                          <Badge tone="warning">Not in the live catalogue</Badge>
                        </p>
                      )}
                      {row.record?.pricing[0] ? (
                        <p className="mt-1 text-xs tabular-nums text-fg-muted sm:hidden">
                          {priceLabel(row.record.pricing[0])}
                        </p>
                      ) : null}
                      {/* Below `sm` the price and the provider ride under the
                          model: four columns in ~240px squeezed the picker to
                          one letter. */}
                      {on ? (
                        <div className="mt-2 sm:hidden">
                          <ProviderControl row={row} entry={entry} onPin={onPin} />
                        </div>
                      ) : null}
                    </Td>
                    <Td className="hidden tabular-nums text-fg-muted sm:table-cell">
                      {row.record?.pricing[0] ? (
                        <span title={row.record.pricing.map(priceLabel).join("\n")}>
                          {priceLabel(row.record.pricing[0])}
                          {row.record.pricing.length > 1 ? (
                            <span className="text-fg-subtle"> +{row.record.pricing.length - 1}</span>
                          ) : null}
                        </span>
                      ) : (
                        "—"
                      )}
                    </Td>
                    <Td className="hidden sm:table-cell">
                      {on ? (
                        <ProviderControl row={row} entry={entry} onPin={onPin} />
                      ) : (
                        <span className="text-sm text-fg-subtle">—</span>
                      )}
                    </Td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
        </div>
      )}
    </section>
  );
}

/**
 * The optional provider pin (§9.2): offered only for a model the catalogue
 * lists under more than one provider tag. Pinning one sets
 * `allow_fallbacks=false` on every request for it (§9.1 rule 6).
 */
function ProviderControl({
  row,
  entry,
  onPin,
}: {
  row: ModelRow;
  entry: MediaAllowlistEntry | undefined;
  onPin: (modelId: string, providerTag: string | null) => void;
}) {
  if (row.providers.length === 0) {
    return <span className="text-sm text-fg-subtle">Any provider</span>;
  }
  return (
    <Select
      aria-label={`Provider for ${row.modelId}`}
      value={entry?.provider_tag ?? ""}
      onChange={(event) => onPin(row.modelId, event.target.value || null)}
    >
      <option value="">Any provider</option>
      {row.providers.map((tag) => (
        <option key={tag} value={tag}>
          {tag}
        </option>
      ))}
    </Select>
  );
}
