"use client";

import { useState } from "react";

import { Alert } from "@/components/ui/alert";
import { CopyButton } from "@/components/ui/copy-button";
import { SegmentedControl } from "@/components/ui/segmented";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api";
import { useLandingPatch } from "@/lib/queries";

type Format = "html" | "json";

/**
 * `PatchViewer` — the landing-page change 4.5.2 proposes, as the site owner
 * receives it (Stage 04 PRD §15.4 J, law 41): the HTML snippet, or the same
 * change as JSON, each copied whole with one click.
 *
 * Shown as text, never rendered: the snippet is markup for someone else's
 * site, and the api serves it inert (`sandbox` CSP) for the same reason.
 */
export function PatchViewer({ auditId, url }: { auditId: string; url: string }) {
  const [format, setFormat] = useState<Format>("html");
  const html = useLandingPatch(auditId, "html");
  const json = useLandingPatch(auditId, "json", format === "json");
  const query = format === "html" ? html : json;
  const text =
    query.data === undefined ? null : typeof query.data === "string" ? query.data : JSON.stringify(query.data, null, 2);
  const titleId = `patch-${auditId}-title`;

  return (
    <section aria-labelledby={titleId} className="flex min-w-0 flex-col gap-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <h3 id={titleId} className="text-sm font-medium text-fg">
            Patch for the site owner
          </h3>
          <p className="text-xs text-fg-muted">Nothing on {hostOf(url)} is changed from here. Hand this over.</p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <SegmentedControl<Format>
            label="Patch format"
            value={format}
            onChange={setFormat}
            options={[
              { value: "html", label: "HTML" },
              { value: "json", label: "JSON" },
            ]}
          />
          {text !== null ? <CopyButton value={text} label={format === "html" ? "Copy HTML" : "Copy JSON"} /> : null}
        </div>
      </div>
      {query.isPending ? (
        <Skeleton className="h-40 w-full" />
      ) : query.error ? (
        <Alert tone="error" title="The patch could not be loaded">
          {query.error instanceof ApiError ? query.error.detail : "Refresh the page to try again."}
        </Alert>
      ) : (
        <pre
          tabIndex={0}
          aria-label={`Patch as ${format.toUpperCase()}`}
          data-testid={`patch-${format}`}
          className="max-h-80 overflow-auto rounded-token border bg-surface p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap text-fg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        >
          <code>{text}</code>
        </pre>
      )}
    </section>
  );
}

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}
