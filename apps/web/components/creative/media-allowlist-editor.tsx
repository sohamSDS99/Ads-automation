"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Film, Image as ImageIcon, type LucideIcon } from "lucide-react";
import { useMemo, useState } from "react";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import { absoluteTime, relativeTime } from "@/lib/format";
import {
  EMPTY_ALLOWLIST,
  capabilityChips,
  priceLabel,
  putMediaSettings,
  sortedPrices,
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
 * an operator may choose when starting a run. Nothing is allowed until
 * somebody allows it — there is no seed default for IMAGE_GEN or VIDEO_GEN
 * (law 36) — so the empty state is the first state, and it says what it stops.
 *
 * Tables, because a catalogue is data (§15.2 rule 4); one card around them,
 * because saving the list is a decision. Before the save, the footer states
 * what the save does in numbers (§15.2 rule 8). The catalogue runs to dozens
 * of models per modality (55 image, 29 video when S4-P1 recorded it), so each
 * table has a filter.
 *
 * No provider pin is offered: `GET /media/catalogue` carries OpenRouter's
 * per-model summaries, which have no provider tags, and video cannot pin one
 * at all. A pin already on an entry is shown and kept through every save.
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

  // Only the allowlist is sent: `PUT /settings/media` leaves a section it is
  // not given as it is, so the workspace's `media_defaults` are untouched.
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

  function toggle(modality: MediaModality, modelId: string, on: boolean) {
    const entries = current[modality];
    const existing = entries.find((entry) => entry.model_id === modelId);
    const wasSaved = saved[modality].some((entry) => entry.model_id === modelId);
    let next: MediaAllowlistEntry[];
    if (on) {
      next = existing
        ? entries.map((entry) => (entry.model_id === modelId ? { ...entry, enabled: true } : entry))
        : [...entries, { model_id: modelId, provider_tag: null, enabled: true }];
    } else if (wasSaved) {
      // A saved entry is switched off rather than deleted, so a provider pin
      // set on it survives being turned off and on again.
      next = entries.map((entry) => (entry.model_id === modelId ? { ...entry, enabled: false } : entry));
    } else {
      next = entries.filter((entry) => entry.model_id !== modelId);
    }
    setDraft({ ...current, [modality]: next });
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
        />
        <ModalityTable
          modality="video"
          title="Video models"
          icon={Film}
          catalogue={video}
          entries={loading ? undefined : current.video}
          onToggle={(id, on) => toggle("video", id, on)}
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

/** One model: its catalogue record, or `null` for a listed model the catalogue dropped. */
type ModelRow = { modelId: string; record: CapabilityRecord | null };

function rowsFor(catalogue: MediaCatalogue | undefined, entries: MediaAllowlistEntry[]): ModelRow[] {
  const byId = new Map<string, ModelRow>();
  for (const record of catalogue?.models ?? []) {
    if (!byId.has(record.model_id)) byId.set(record.model_id, { modelId: record.model_id, record });
  }
  // A model on the list that the catalogue no longer carries is still shown,
  // so it can be taken off: a run cannot use it (CR-E8), and an allowlist
  // that hides its own dead entries is one nobody can clean up.
  if (catalogue) {
    for (const entry of entries) {
      if (!byId.has(entry.model_id)) byId.set(entry.model_id, { modelId: entry.model_id, record: null });
    }
  }
  return [...byId.values()].sort((a, b) => a.modelId.localeCompare(b.modelId));
}

/** What the price cell says. An image summary carries no price; its endpoints do. */
function Price({ record }: { record: CapabilityRecord | null }) {
  if (!record) return <>—</>;
  const lines = sortedPrices(record);
  const first = lines[0];
  if (!first) {
    return <>{record.modality === "image" ? "Priced per provider" : "No price listed"}</>;
  }
  return (
    <span title={lines.map(priceLabel).join("\n")}>
      {lines.length > 1 ? "from " : ""}
      {priceLabel(first)}
    </span>
  );
}

function ModalityTable({
  modality,
  title,
  icon: Icon,
  catalogue,
  entries,
  onToggle,
}: {
  modality: MediaModality;
  title: string;
  icon: LucideIcon;
  catalogue: ReturnType<typeof useMediaCatalogue>;
  entries: MediaAllowlistEntry[] | undefined;
  onToggle: (modelId: string, on: boolean) => void;
}) {
  const [query, setQuery] = useState("");
  const rows = useMemo(() => rowsFor(catalogue.data, entries ?? []), [catalogue.data, entries]);
  const needle = query.trim().toLowerCase();
  // An allowed model always stays in view, so filtering never hides what the
  // save is about to write.
  const visible = needle
    ? rows.filter(
        (row) =>
          row.modelId.toLowerCase().includes(needle) ||
          (entries ?? []).some((entry) => entry.model_id === row.modelId && entry.enabled),
      )
    : rows;
  const enabled = (entries ?? []).filter((entry) => entry.enabled).length;
  const headingId = `media-${modality}`;

  return (
    <section aria-labelledby={headingId} className="flex min-w-0 flex-col gap-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id={headingId} className="flex items-center gap-2 text-sm font-medium text-fg">
          <Icon className="size-4 text-fg-subtle" aria-hidden />
          {title}
        </h3>
        {entries && catalogue.data ? (
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

      {catalogue.isError ? (
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
        <>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <Input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={`Filter ${rows.length} ${modality} models`}
              aria-label={`Filter ${modality} models`}
              className="w-full sm:w-72"
            />
            <p className="text-xs text-fg-subtle">
              Live catalogue, read{" "}
              <time dateTime={catalogue.data.fetched_at} title={absoluteTime(catalogue.data.fetched_at)}>
                {relativeTime(catalogue.data.fetched_at)}
              </time>
            </p>
          </div>
          <div className="min-w-0 rounded-token border">
            {/* `min-w-0` undoes `Table`'s `min-w-max`, so a long model id wraps
                inside its cell instead of widening the table past a phone. */}
            <Table label={`${title} in the live catalogue`} className="min-w-0">
              <thead>
                <Tr>
                  <Th className="w-0">Allow</Th>
                  <Th>Model</Th>
                  <Th className="hidden sm:table-cell">Price</Th>
                </Tr>
              </thead>
              <tbody>
                {visible.map((row) => {
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
                        <p className="mt-1 flex flex-wrap gap-1">
                          {row.record ? (
                            capabilityChips(row.record).map((chip) => (
                              <Badge key={chip}>
                                <span className="tabular-nums">{chip}</span>
                              </Badge>
                            ))
                          ) : (
                            <Badge tone="warning">Not in the live catalogue</Badge>
                          )}
                          {entry?.provider_tag ? (
                            <Badge tone="accent">Pinned to {entry.provider_tag}</Badge>
                          ) : null}
                        </p>
                        {/* Below `sm` the price rides under the model: three
                            columns in ~240px leave the id a word per line. */}
                        <p className="mt-1 text-xs tabular-nums text-fg-muted sm:hidden">
                          <Price record={row.record} />
                        </p>
                      </Td>
                      <Td className="hidden text-sm tabular-nums text-fg-muted sm:table-cell">
                        <Price record={row.record} />
                      </Td>
                    </Tr>
                  );
                })}
                {visible.length === 0 ? (
                  <Tr>
                    <Td colSpan={3} className="text-sm text-fg-muted">
                      No {modality} model matches “{query.trim()}”.
                    </Td>
                  </Tr>
                ) : null}
              </tbody>
            </Table>
          </div>
        </>
      )}
    </section>
  );
}
