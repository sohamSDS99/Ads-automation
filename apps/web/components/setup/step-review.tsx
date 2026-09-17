"use client";

import { useQuery } from "@tanstack/react-query";
import { CircleAlert, CircleCheck, TriangleAlert } from "lucide-react";

import { RunTrigger } from "@/components/projects/run-trigger";
import { Alert } from "@/components/ui/alert";
import { listDocuments } from "@/lib/api/documents";
import { blockers, warnings, type ProjectDetail } from "@/lib/api/projects";
import { keys } from "@/lib/queries";
import { Can } from "@/lib/session";

/**
 * Step 5 — what is about to happen, and whether it can.
 *
 * The button's disabled state and the list above it come from the same server
 * field, so the summary can never say "ready" while the API refuses the launch.
 */
export function StepReview({ project }: { project: ProjectDetail }) {
  const stopped = blockers(project);
  const degraded = warnings(project);
  const context = project.product_context;
  // Listed here because it is the one part of step 1 that saves itself as you
  // go: someone who uploaded a file two screens ago should see it counted in
  // the summary of what the run will read.
  const library = useQuery({
    queryKey: keys.documents(project.id),
    queryFn: () => listDocuments(project.id),
  });
  const documents = library.data?.documents ?? [];

  return (
    <div className="space-y-5">
      <dl className="grid gap-x-6 gap-y-4 sm:grid-cols-2">
        <Row term="Project">{project.name}</Row>
        <Row term="Domain">
          <span className="font-mono text-sm">{project.domain}</span>
        </Row>
        <Row term="Markets">
          {project.markets.length
            ? project.markets.map((market) => market.country).join(", ")
            : "None set"}
        </Row>
        <Row term="Products">
          {context.products.length ? context.products.join(", ") : "None listed"}
        </Row>
        <Row term="Models">
          <span className="font-mono text-xs">
            {[project.models.extract, project.models.synthesize].filter(Boolean).join(", ") ||
              "Workspace defaults"}
          </span>
        </Row>
        <Row term="Gates">
          {project.gates.filter((gate) => gate.assignee_name).length} of {project.gates.length}{" "}
          assigned
        </Row>
        <Row term="Background documents">
          {documents.length
            ? `${documents.length} file${documents.length === 1 ? "" : "s"} · ${documents
                .reduce((total, item) => total + item.passage_count, 0)
                .toLocaleString()} citable passages`
            : "None uploaded"}
        </Row>
      </dl>

      <div>
        <h3 className="text-xs font-medium tracking-wide text-fg-subtle">Business context</h3>
        <p className="mt-1.5 max-w-prose whitespace-pre-wrap text-sm leading-relaxed text-fg-muted">
          {context.summary || "Nothing written yet."}
        </p>
      </div>

      {stopped.length ? (
        <Alert tone="error" title="Not ready to run">
          <ul className="space-y-1">
            {stopped.map((item) => (
              <li key={item.code} className="flex gap-2">
                <CircleAlert className="mt-0.5 size-3.5 shrink-0 text-status-failed" aria-hidden />
                {item.detail}
              </li>
            ))}
          </ul>
        </Alert>
      ) : null}

      {degraded.length ? (
        <Alert tone="warning" title="The run will start, with less evidence">
          <ul className="space-y-1">
            {degraded.map((item) => (
              <li key={item.code} className="flex gap-2">
                <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-status-gate" aria-hidden />
                {item.detail}
              </li>
            ))}
          </ul>
        </Alert>
      ) : null}

      {!stopped.length && !degraded.length ? (
        <div className="flex items-center gap-2.5 rounded-[var(--radius)] border border-status-success/40 bg-surface px-3.5 py-3 text-sm">
          <CircleCheck className="size-4 shrink-0 text-status-success" aria-hidden />
          <span className="text-fg">
            Everything this project needs is in place. A full run takes under 45 minutes.
          </span>
        </div>
      ) : null}

      <Can
        permission="run_execute"
        fallback={
          <p className="text-sm text-fg-muted">
            Setup is saved. Launching a run is an operator or admin action.
          </p>
        }
      >
        <RunTrigger project={project} />
      </Can>
    </div>
  );
}

function Row({ term, children }: { term: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs font-medium tracking-wide text-fg-subtle">{term}</dt>
      <dd className="mt-1 text-sm text-fg">{children}</dd>
    </div>
  );
}
