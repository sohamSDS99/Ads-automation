"use client";

/**
 * "Compare with previous run" (PRD §13.4 C).
 *
 * The question this answers is never "what does the report say" — the report is
 * right there — it is "what moved since last time". So the panel leads with the
 * handful of single values that decide something (the launch verdict above all)
 * and only then lists the records that changed, section by section.
 *
 * Sections with nothing to say are not rendered at all. The API omits them, and
 * a column of twenty-six "no change" rows would bury the three that matter.
 *
 * Where the API says it capped a list, the panel says so too. A cap that reads
 * as "and nothing else changed" is worse than no comparison.
 */

import { Alert } from "@/components/ui/alert";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { DiffBody } from "@/components/ui/diff";
import { Skeleton } from "@/components/ui/skeleton";
import type { RunDiff } from "@/lib/api/diff";
import { absoluteTime, relativeTime } from "@/lib/format";

export function ComparePanel({
  diff,
  pending,
  error,
}: {
  diff: RunDiff | undefined;
  pending: boolean;
  error: string | null;
}) {
  if (error) {
    return (
      <Alert tone="warning" title="Nothing to compare">
        {error}
      </Alert>
    );
  }
  if (pending || !diff) return <Skeleton className="h-40 w-full" />;

  return (
    <Card>
      <CardHeader
        title="Changes since the previous run"
        description={
          <>
            Comparing this report, generated{" "}
            <time dateTime={diff.generated_at} title={absoluteTime(diff.generated_at)}>
              {relativeTime(diff.generated_at)}
            </time>
            , with the run from{" "}
            <time
              dateTime={diff.against_generated_at}
              title={absoluteTime(diff.against_generated_at)}
            >
              {relativeTime(diff.against_generated_at)}
            </time>
            {diff.against_is_parent ? "" : " that you selected"}.
          </>
        }
      />
      <CardBody>
        <DiffBody
          scalars={diff.scalars}
          sections={diff.sections}
          unchanged={diff.unchanged}
          unchangedNote="Nothing the report tracks differs between these two runs. Citations are not compared — evidence is re-gathered every run, so every one of them is new by definition."
        />
      </CardBody>
    </Card>
  );
}

