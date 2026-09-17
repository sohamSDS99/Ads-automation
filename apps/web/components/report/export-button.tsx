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
const FORMATS: ExportFormat[] = ["pdf", "docx", "md", "json", "csv"];
const POLL_MS = 1500;
const GIVE_UP_AFTER_MS = 5 * 60 * 1000;

export function ExportButton({ runId }: { runId: string }) {
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState<ExportFormat | null>(null);

  async function run(format: ExportFormat) {
    setBusy(format);
    const toastId = toast.loading(`Building the ${FORMAT_LABEL[format]}`, {
      description: "This runs in the worker; it takes a few seconds.",
      duration: Number.POSITIVE_INFINITY,
    });
    try {
      const accepted = await requestExport(runId, format);
      const finished = await waitForExport(accepted.job_id);

      if (finished.status === "ready") {
        toast.success(`${FORMAT_LABEL[format]} ready`, {
          id: toastId,
          description: finished.filename ?? undefined,
          duration: 30_000,
          action: {
            label: "Download",
            onClick: () => window.open(downloadUrl(finished.id), "_blank", "noopener"),
          },
        });
      } else {
        toast.error(`The ${FORMAT_LABEL[format]} could not be generated`, {
          id: toastId,
          description: finished.error ?? "The worker reported a failure.",
          duration: 20_000,
        });
      }
      await queryClient.invalidateQueries({ queryKey: keys.report(runId) });
    } catch (error) {
      toast.error(`The ${FORMAT_LABEL[format]} could not be requested`, {
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
        onClick={() => void run("pdf")}
        disabled={busy !== null}
        className="rounded-r-none"
        title="Export as PDF"
      >
        {busy === "pdf" ? <Spinner label="Building" /> : <Download aria-hidden />}
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
          {FORMATS.map((format) => (
            <DropdownMenuItem key={format} onSelect={() => void run(format)}>
              {FORMAT_LABEL[format]}
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
