"use client";

import { useQueryClient } from "@tanstack/react-query";
import { ChevronDown, Download } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import {
  FORMAT_LABEL,
  downloadUrl,
  getExport,
  requestExport,
  type ExportFormat,
} from "@/lib/api/reports";
import { keys } from "@/lib/queries";

/**
 * Take the report away, in any of the five formats (PRD §12, §13.4 C).
 *
 * Generation happens in the worker, so this is a request and a wait, not a
 * download. The toast is the wait: it stays up, says which format is being
 * built, and turns into the download when the job reports ready. Nothing
 * auto-downloads — a file that saves itself while someone is reading is a
 * surprise, and a browser blocks it half the time anyway.
 */
const FORMATS = ["pdf", "docx", "md", "json", "csv"] as const satisfies readonly ExportFormat[];
const POLL_MS = 1500;
const GIVE_UP_AFTER_MS = 5 * 60 * 1000;

/**
 * The Stage 03 rulebook's formats (`export.jobs.CONTENT_GUIDELINE_FORMATS`).
 *
 * Not the same set as a report's: there is no keyword CSV, and there are two
 * the report has no use for — a spreadsheet of the specs, and the compiled
 * ruleset as JSON, which is what somebody hands to a tool rather than reads.
 */
export type GuidelineExportFormat = "pdf" | "docx" | "md" | "json" | "xlsx" | "ruleset_json";

export const GUIDELINE_FORMAT_LABEL: Record<GuidelineExportFormat, string> = {
  pdf: "PDF",
  docx: "Word",
  md: "Markdown",
  json: "JSON",
  xlsx: "Spreadsheet",
  ruleset_json: "Compiled ruleset (JSON)",
};

export function ExportButton({ runId }: { runId: string }) {
  return (
    <ExportControl
      formats={FORMATS}
      label={(format) => FORMAT_LABEL[format]}
      request={(format) => requestExport(runId, format).then((accepted) => accepted.job_id)}
      invalidate={keys.report(runId)}
    />
  );
}

/**
 * The generic half: request, wait, offer the file.
 *
 * Split out of `ExportButton` when Stage 03 arrived with a different format
 * list and a different endpoint but the identical wait. Everything that
 * differs is a prop; everything that is the same — the toast that stays up,
 * the poll, the five-minute give-up, the deliberate absence of an
 * auto-download — is here once.
 */
export function ExportControl<F extends string>({
  formats,
  label,
  request,
  invalidate,
  primary,
}: {
  /** Non-empty by type, so the split button's main half always has a format. */
  formats: readonly [F, ...F[]];
  label: (format: F) => string;
  /** Kick off the job; resolve with its id. */
  request: (format: F) => Promise<string>;
  /** The read key the finished job may have changed. */
  invalidate: readonly unknown[];
  /** The format the main half of the split button runs. Defaults to the first. */
  primary?: F;
}) {
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState<F | null>(null);
  const main = primary ?? formats[0];

  async function run(format: F) {
    setBusy(format);
    const toastId = toast.loading(`Building the ${label(format)}`, {
      description: "This runs in the worker; it takes a few seconds.",
      duration: Number.POSITIVE_INFINITY,
    });
    try {
      const jobId = await request(format);
      const finished = await waitForExport(jobId);

      if (finished.status === "ready") {
        toast.success(`${label(format)} ready`, {
          id: toastId,
          description: finished.filename ?? undefined,
          duration: 30_000,
          action: {
            label: "Download",
            onClick: () => window.open(downloadUrl(finished.id), "_blank", "noopener"),
          },
        });
      } else {
        toast.error(`The ${label(format)} could not be generated`, {
          id: toastId,
          description: finished.error ?? "The worker reported a failure.",
          duration: 20_000,
        });
      }
      await queryClient.invalidateQueries({ queryKey: invalidate });
    } catch (error) {
      toast.error(`The ${label(format)} could not be requested`, {
        id: toastId,
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
        duration: 20_000,
      });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="flex">
      <Button
        onClick={() => void run(main)}
        disabled={busy !== null}
        className="rounded-r-none"
        title={`Export as ${label(main)}`}
      >
        {busy === main ? <Spinner label="Building" /> : <Download aria-hidden />}
        Export
      </Button>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            disabled={busy !== null}
            className="rounded-l-none border-l border-l-accent-fg/20 px-2"
          >
            <ChevronDown aria-hidden />
            <span className="sr-only">Choose a format</span>
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent>
          {formats.map((format) => (
            <DropdownMenuItem key={format} onSelect={() => void run(format)}>
              {label(format)}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}

/**
 * Poll until the job settles.
 *
 * `export.ready` also arrives on the run's SSE channel, but only to a console
 * that happens to be open — PRD §12 names this endpoint as the reliable answer,
 * so this is the one the button trusts.
 */
async function waitForExport(jobId: string) {
  const deadline = Date.now() + GIVE_UP_AFTER_MS;
  for (;;) {
    const job = await getExport(jobId);
    if (job.status === "ready" || job.status === "failed") return job;
    if (Date.now() > deadline) {
      return { ...job, status: "failed" as const, error: "It is taking longer than five minutes." };
    }
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
  }
}
