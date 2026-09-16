"use client";

import { FileSearch } from "lucide-react";

import { EmptyState } from "@/components/ui/empty-state";

/**
 * The evidence explorer is built in the next phase. The route exists now so the
 * navigation every role sees leads somewhere that explains itself.
 */
export default function EvidencePage() {
  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6">
      <header>
        <h1 className="text-[length:var(--text-xl)] font-semibold tracking-tight">Evidence</h1>
        <p className="mt-1 max-w-prose text-sm text-fg-muted">
          Every fact a run works from is stored with its source and the time it was fetched. No
          claim in a report exists without at least one row here behind it.
        </p>
      </header>

      <EmptyState
        icon={FileSearch}
        title="The explorer ships in the next phase"
        description="Evidence is already being collected — CSV imports on a project's setup screen write rows today. Searching and reading them gets its own screen alongside the run console."
      />
    </div>
  );
}
