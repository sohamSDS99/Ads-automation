"use client";

import { FlaskConical } from "lucide-react";
import { useParams } from "next/navigation";

import { LinterPlayground } from "@/components/lint/playground";
import { Alert } from "@/components/ui/alert";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessage, useGuidelines } from "@/lib/queries";

/**
 * `/projects/[id]/guidelines/lint` — ask the rulebook a question.
 *
 * Open to every role including `viewer`: the route has no side effects, makes
 * no model call and writes nothing, so gating it would withhold the rulebook
 * from exactly the people who most need to check against it before asking
 * anybody for anything (PRD §15.3 F).
 */
export default function LintPage() {
  const params = useParams<{ id: string }>();
  const versions = useGuidelines(params.id);
  const list = versions.data?.versions ?? [];
  const current = list.find((item) => item.status === "published") ?? list[0];

  return (
    <div className="flex flex-col gap-6">
      <header>
        <h1 className="text-[length:var(--text-xl)] font-semibold tracking-tight">
          Check your copy
        </h1>
        <p className="mt-1 max-w-prose text-sm text-fg-muted">
          Paste a headline and see what the rulebook says about it, with the offending words
          marked and the rule that objects named. Nothing here is saved and nothing is
          submitted — it is the rulebook answering a question.
        </p>
      </header>

      {versions.isLoading ? (
        <Skeleton className="h-64 w-full" />
      ) : versions.isError ? (
        <Alert tone="error" title="This project's guidelines could not be read">
          {errorMessage(versions)}
        </Alert>
      ) : !current ? (
        <EmptyState
          icon={FlaskConical}
          title="No rulebook to check against yet"
          description="Rules are compiled by a guideline run. Start one from the Content Guidelines tab and this screen becomes usable while it is still a draft."
        />
      ) : (
        <LinterPlayground guidelineId={current.id} />
      )}
    </div>
  );
}
