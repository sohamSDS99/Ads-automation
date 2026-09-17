"use client";

/**
 * The schedule editor (PRD §13.4 E).
 *
 * A cron expression is a thing people get wrong silently — `0 0 * * 0` and
 * `0 0 * * 1` differ by one character and a whole day — so the editor never
 * lets the field stand alone. Every keystroke is answered by the server's own
 * parser, in a sentence and in the next three instants it will actually fire.
 * The preview is the product here; the text field is just how you address it.
 *
 * Deliberately not a modal: creating a schedule needs neither interruption nor
 * protected focus, and a person comparing "what I typed" against "what it will
 * do" wants the existing schedules still on screen while they do it.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, Pause, Play, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { Field } from "@/components/ui/field";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import type { ProjectSummary } from "@/lib/api/projects";
import {
  CRON_PRESETS,
  createSchedule,
  deleteSchedule,
  localTimezone,
  previewSchedule,
  timezoneOptions,
  updateSchedule,
  type Schedule,
  type SchedulePreview,
} from "@/lib/api/schedules";
import { absoluteTime, relativeTime } from "@/lib/format";
import { errorMessage, keys, useSchedules } from "@/lib/queries";

/** Long enough that a half-typed expression is not sent, short enough to feel live. */
const PREVIEW_DEBOUNCE_MS = 350;

export function ScheduleEditor({ projects }: { projects: ProjectSummary[] }) {
  const schedules = useSchedules();
  const error = errorMessage(schedules);
  const rows = schedules.data?.items ?? [];

  return (
    <Card>
      <CardHeader
        title="Scheduled runs"
        description="A scheduled run has no person behind it: it triggers itself, spends from the workspace budget, and shows as “Schedule” in the run history."
      />
      <CardBody className="space-y-5">
        {error ? (
          <Alert tone="error" title="Schedules could not be loaded">
            {error}
          </Alert>
        ) : null}

        {schedules.isPending ? <Skeleton className="h-24 w-full" /> : null}

        {!schedules.isPending && rows.length === 0 ? (
          <EmptyState
            icon={CalendarClock}
            title="Nothing is scheduled"
            description="Research runs only happen when someone launches one. Add a schedule to have a project re-run on its own."
          />
        ) : null}

        {rows.length > 0 ? <ScheduleTable rows={rows} /> : null}
      </CardBody>
      <CardFooter className="justify-start">
        <NewSchedule projects={projects} />
      </CardFooter>
    </Card>
  );
}

function ScheduleTable({ rows }: { rows: Schedule[] }) {
  const client = useQueryClient();
  const [pendingDelete, setPendingDelete] = useState<Schedule | null>(null);

  const toggle = useMutation({
    mutationFn: (row: Schedule) => updateSchedule(row.id, { enabled: !row.enabled }),
    onSuccess: (row) => {
      void client.invalidateQueries({ queryKey: keys.schedules() });
      toast.success(row.enabled ? "Schedule resumed" : "Schedule paused");
    },
    onError: (cause: unknown) =>
      toast.error(cause instanceof ApiError ? cause.detail : "That could not be saved."),
  });

  const remove = useMutation({
    mutationFn: (row: Schedule) => deleteSchedule(row.id),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.schedules() });
      toast.success("Schedule deleted");
    },
    onError: (cause: unknown) =>
      toast.error(cause instanceof ApiError ? cause.detail : "That could not be deleted."),
  });

  return (
    <>
      <Table label="Scheduled runs, soonest first">
        <thead>
          <Tr>
            <Th>Project</Th>
            <Th>Runs</Th>
            <Th>Next</Th>
            <Th>Last</Th>
            <Th className="text-right">Actions</Th>
          </Tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <Tr key={row.id}>
              <Td className="font-medium text-fg">{row.project_name}</Td>
              <Td>
                <span className="text-fg">{row.description}</span>
                {/* The expression itself stays visible: it is what an admin
                    edits, and what they will paste into a support thread. */}
                <span className="ml-2 font-mono text-xs text-fg-subtle">{row.cron}</span>
              </Td>
              <Td>
                {row.enabled && row.next_at ? (
                  <time dateTime={row.next_at} title={absoluteTime(row.next_at)}>
                    {relativeTime(row.next_at)}
                  </time>
                ) : (
                  <span className="text-fg-muted">Paused</span>
                )}
              </Td>
              <Td>
                {row.last_run_at ? (
                  <time dateTime={row.last_run_at} title={absoluteTime(row.last_run_at)}>
                    {relativeTime(row.last_run_at)}
                  </time>
                ) : (
                  <span className="text-fg-muted">Never</span>
                )}
              </Td>
              <Td className="text-right">
                <div className="flex justify-end gap-1">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => toggle.mutate(row)}
                    disabled={toggle.isPending}
                  >
                    {row.enabled ? (
                      <>
                        <Pause aria-hidden /> Pause
                      </>
                    ) : (
                      <>
                        <Play aria-hidden /> Resume
                      </>
                    )}
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => setPendingDelete(row)}>
                    <Trash2 aria-hidden />
                    <span className="sr-only">Delete schedule for {row.project_name}</span>
                  </Button>
                </div>
              </Td>
            </Tr>
          ))}
        </tbody>
      </Table>

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => !open && setPendingDelete(null)}
        title="Delete this schedule?"
        description="Runs already completed by it are kept. Only the schedule goes."
        body={
          pendingDelete ? (
            <p className="text-sm text-fg-muted">
              <span className="text-fg">{pendingDelete.project_name}</span> will stop running{" "}
              {pendingDelete.description.toLowerCase()}
            </p>
          ) : null
        }
        confirmLabel="Delete schedule"
        destructive
        onConfirm={async () => {
          if (pendingDelete) await remove.mutateAsync(pendingDelete);
          setPendingDelete(null);
        }}
      />
    </>
  );
}

function NewSchedule({ projects }: { projects: ProjectSummary[] }) {
  const client = useQueryClient();
  const zones = useMemo(timezoneOptions, []);
  const [open, setOpen] = useState(false);
  const [projectId, setProjectId] = useState(projects[0]?.id ?? "");
  const [cron, setCron] = useState(CRON_PRESETS[0]?.cron ?? "0 7 * * 1");
  const [timezone, setTimezone] = useState(localTimezone);

  const preview = usePreview(cron, timezone, open);

  const create = useMutation({
    mutationFn: () =>
      createSchedule({ project_id: projectId, cron, timezone, enabled: true }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.schedules() });
      toast.success("Schedule created");
      setOpen(false);
    },
    onError: (cause: unknown) =>
      toast.error(cause instanceof ApiError ? cause.detail : "That could not be saved."),
  });

  if (!open) {
    return (
      <Button variant="secondary" onClick={() => setOpen(true)} disabled={projects.length === 0}>
        <CalendarClock aria-hidden />
        Add a schedule
      </Button>
    );
  }

  return (
    <div className="w-full space-y-4">
      <div className="grid gap-4 sm:grid-cols-3">
        <label className="flex flex-col gap-1.5 text-sm">
          <span className="font-medium text-fg">Project</span>
          <Select value={projectId} onChange={(event) => setProjectId(event.target.value)}>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </Select>
        </label>

        <Field
          label="Cron expression"
          value={cron}
          onChange={(event) => setCron(event.target.value)}
          spellCheck={false}
          autoComplete="off"
          className="font-mono"
          error={preview.error ?? undefined}
          hint="minute hour day-of-month month day-of-week"
        />

        <label className="flex flex-col gap-1.5 text-sm">
          <span className="font-medium text-fg">Timezone</span>
          <Select value={timezone} onChange={(event) => setTimezone(event.target.value)}>
            {zones.map((zone) => (
              <option key={zone} value={zone}>
                {zone}
              </option>
            ))}
          </Select>
        </label>
      </div>

      <div className="flex flex-wrap gap-2">
        {CRON_PRESETS.map((preset) => (
          <Button
            key={preset.cron}
            variant="ghost"
            size="sm"
            onClick={() => setCron(preset.cron)}
            aria-pressed={cron === preset.cron}
            className={cron === preset.cron ? "bg-surface-hover text-fg" : undefined}
          >
            {preset.label}
          </Button>
        ))}
      </div>

      <SchedulePreviewPanel preview={preview.data} pending={preview.pending} />

      <div className="flex gap-2">
        <Button
          onClick={() => create.mutate()}
          disabled={create.isPending || Boolean(preview.error) || !projectId}
        >
          Create schedule
        </Button>
        <Button variant="ghost" onClick={() => setOpen(false)}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

/**
 * What this expression means, in the server's words.
 *
 * Shown as a sentence *and* as instants. The sentence is checkable at a glance;
 * the instants are what catch the mistake the sentence reads past — a timezone
 * that puts "every night at 02:00" an hour off what the admin pictured.
 */
function SchedulePreviewPanel({
  preview,
  pending,
}: {
  preview: SchedulePreview | null;
  pending: boolean;
}) {
  if (!preview) {
    return (
      <div className="rounded-[var(--radius)] border border-dashed px-4 py-3 text-sm text-fg-muted">
        {pending ? "Checking…" : "Enter an expression to see when it runs."}
      </div>
    );
  }
  return (
    <div
      aria-live="polite"
      className="rounded-[var(--radius)] border bg-surface px-4 py-3"
      data-pending={pending || undefined}
    >
      <p className="text-sm text-fg">{preview.description}</p>
      {preview.upcoming.length ? (
        <ol className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs text-fg-muted">
          {preview.upcoming.map((instant, index) => (
            <li key={instant} className="flex items-baseline gap-1.5">
              <span className="text-fg-subtle">{index === 0 ? "next" : `+${index}`}</span>
              <time dateTime={instant} className="tabular-nums text-fg-muted">
                {absoluteTime(instant)}
              </time>
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  );
}

/** Debounced server-side validation. The only parser this UI trusts. */
function usePreview(cron: string, timezone: string, enabled: boolean) {
  const [data, setData] = useState<SchedulePreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  useEffect(() => {
    if (!enabled || !cron.trim()) {
      setData(null);
      setError(null);
      return;
    }
    let cancelled = false;
    setPending(true);
    const timer = setTimeout(() => {
      previewSchedule({ cron, timezone })
        .then((result) => {
          if (cancelled) return;
          setData(result);
          setError(null);
        })
        .catch((cause: unknown) => {
          if (cancelled) return;
          // The server's message names the field and the fix ("'60' is outside
          // 0-59 for minute"), which is more use than anything generic here.
          setData(null);
          setError(cause instanceof ApiError ? cause.detail : "That expression is not valid.");
        })
        .finally(() => {
          if (!cancelled) setPending(false);
        });
    }, PREVIEW_DEBOUNCE_MS);

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [cron, timezone, enabled]);

  return { data, error, pending };
}
