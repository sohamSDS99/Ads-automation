"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, Check } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { StepApprovers, type GateDraft } from "@/components/setup/step-approvers";
import { StepContext } from "@/components/setup/step-context";
import { StepModels } from "@/components/setup/step-models";
import { StepReview } from "@/components/setup/step-review";
import { StepSources } from "@/components/setup/step-sources";
import { Stepper, type Step } from "@/components/setup/stepper";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import {
  autofillProject,
  updateProject,
  type AutofillField,
  type AutofillFinding,
  type Market,
  type ModelRouting,
  type ProductContext,
  type ProjectDetail,
  type ProjectPatch,
} from "@/lib/api/projects";
import { keys } from "@/lib/queries";
import { useSession } from "@/lib/session";

const TITLES = [
  { title: "Business context", blurb: "What this brand sells, and where." },
  { title: "Data sources", blurb: "Where the evidence comes from." },
  { title: "Model routing", blurb: "Which model does which job, and what it costs." },
  { title: "Approvers", blurb: "Who decides the three gates." },
  { title: "Review", blurb: "What is about to happen." },
] as const;

/**
 * The five-step setup, saved a step at a time.
 *
 * Incremental rather than one submit at the end: someone who gets called away
 * on step two comes back to a project that remembers step one. Each step sends
 * only the fields it owns, so two people working on different steps of the same
 * project do not overwrite each other's work — and if they touch the same one,
 * `If-Unmodified-Since` turns it into a 412 and a prompt rather than a silent
 * loss (PRD §16).
 */
export function SetupWizard({ project }: { project: ProjectDetail }) {
  const { has } = useSession();
  const queryClient = useQueryClient();
  const [step, setStep] = useState(0);
  const [stale, setStale] = useState(false);

  const canWriteProject = has("project_write");
  const canWriteSettings = has("settings_write");
  const canWriteCredentials = has("credential_write");

  // Drafts start from the saved project and are keyed to the revision they came
  // from, so a reload after a 412 rebuilds them from the newer row.
  const [context, setContext] = useState<ProductContext>(project.product_context);
  const [markets, setMarkets] = useState<Market[]>(project.markets);
  const [models, setModels] = useState<ModelRouting>(project.models);
  const [gates, setGates] = useState<Record<string, GateDraft>>(() =>
    Object.fromEntries(
      project.gates.map((gate) => [
        gate.node_id,
        { assignee_id: gate.assignee_id, sla_hours: gate.sla_hours },
      ]),
    ),
  );
  // What the last autofill read, kept beside the value so the person can judge
  // the proposal rather than just receive it.
  const [findings, setFindings] = useState<AutofillFinding[]>([]);
  const [version, setVersion] = useState(project.version);
  if (version !== project.version) {
    // A refetch brought a newer row in — adopt it rather than keep editing a
    // copy of the old one. `version` only moves once a write has landed, so
    // this never fires mid-save; assigning during render is what keeps the
    // drafts from lagging one render behind the data.
    setVersion(project.version);
    setContext(project.product_context);
    setMarkets(project.markets);
    setModels(project.models);
    setGates(
      Object.fromEntries(
        project.gates.map((gate) => [
          gate.node_id,
          { assignee_id: gate.assignee_id, sla_hours: gate.sla_hours },
        ]),
      ),
    );
  }

  /**
   * Hand a step-1 field to the agent.
   *
   * The current drafts are saved first, deliberately. Autofill writes on the
   * server, which moves `project.version`, which resyncs every draft on this
   * screen — so anything typed and not yet saved would be replaced by the copy
   * the server still had. Saving first makes that resync a no-op instead of a
   * loss.
   */
  const autofill = useMutation({
    mutationFn: async (field: AutofillField) => {
      const patch = patchFor(0, { context, markets, models, gates });
      if (patch) await save.mutateAsync(patch);
      return autofillProject(project.id, [field]);
    },
    onSuccess: async (result) => {
      setFindings((current) => [
        ...current.filter((item) => !result.findings.some((fresh) => fresh.field === item.field)),
        ...result.findings,
      ]);
      for (const finding of result.findings) {
        if (finding.found) toast.success("Worked it out", { description: finding.source });
        else toast.error("Nothing to read yet", { description: finding.source });
      }
      await queryClient.invalidateQueries({ queryKey: keys.project(project.id) });
    },
    onError: (error) =>
      toast.error("Could not work it out", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      }),
  });

  const save = useMutation({
    mutationFn: (patch: ProjectPatch) => updateProject(project.id, patch, project.version),
    onSuccess: async () => {
      setStale(false);
      await queryClient.invalidateQueries({ queryKey: keys.project(project.id) });
      await queryClient.invalidateQueries({ queryKey: keys.projects });
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 412) {
        setStale(true);
        return;
      }
      toast.error("Not saved", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      });
    },
  });

  const steps: Step[] = [
    {
      id: "context",
      title: TITLES[0].title,
      complete: Boolean(project.product_context.summary && project.markets.length),
      readOnly: !canWriteProject,
    },
    {
      id: "sources",
      title: TITLES[1].title,
      complete: !project.requirements.some((item) => item.code.endsWith("_credential")),
      readOnly: !canWriteProject,
    },
    {
      id: "models",
      title: TITLES[2].title,
      complete: Boolean(project.models.synthesize),
      readOnly: !canWriteSettings,
    },
    {
      id: "approvers",
      title: TITLES[3].title,
      complete: project.gates.some((gate) => gate.assignee_id),
      readOnly: !canWriteProject,
    },
    { id: "review", title: TITLES[4].title },
  ];

  async function saveCurrentStep(): Promise<boolean> {
    const patch = patchFor(step, { context, markets, models, gates });
    if (!patch) return true;
    try {
      await save.mutateAsync(patch);
      toast.success("Saved");
      return true;
    } catch {
      return false;
    }
  }

  async function next() {
    if (await saveCurrentStep()) setStep((index) => Math.min(index + 1, steps.length - 1));
  }

  const meta = TITLES[step];
  const readOnlyStep = steps[step]?.readOnly ?? false;

  return (
    <div className="flex flex-col gap-5">
      <Stepper steps={steps} current={step} onSelect={setStep} />

      {stale ? (
        <Alert tone="warning" title="Someone else edited this project">
          Your changes were not saved, so nothing of theirs was lost. Reload to see the current
          version, then make your edit again.
          <div className="mt-2">
            <Button
              size="sm"
              variant="secondary"
              onClick={async () => {
                await queryClient.invalidateQueries({ queryKey: keys.project(project.id) });
                setStale(false);
              }}
            >
              Reload this project
            </Button>
          </div>
        </Alert>
      ) : null}

      <Card>
        <CardHeader title={meta?.title ?? ""} description={meta?.blurb} />
        <CardBody>
          {step === 0 ? (
            <StepContext
              projectId={project.id}
              context={context}
              markets={markets}
              onContextChange={setContext}
              onMarketsChange={setMarkets}
              disabled={!canWriteProject}
              autofill={project.autofill}
              findings={findings}
              onAutofill={(field) => autofill.mutate(field)}
              onAutofillOff={(field) =>
                save.mutate({ autofill: { ...project.autofill, [field]: false } })
              }
              autofilling={autofill.isPending ? (autofill.variables ?? null) : null}
            />
          ) : null}
          {step === 1 ? (
            <StepSources
              projectId={project.id}
              canWriteCredentials={canWriteCredentials}
              canUpload={canWriteProject}
            />
          ) : null}
          {step === 2 ? (
            <StepModels routing={models} onChange={setModels} canWrite={canWriteSettings} />
          ) : null}
          {step === 3 ? (
            <StepApprovers
              gates={project.gates}
              drafts={gates}
              onChange={setGates}
              disabled={!canWriteProject}
            />
          ) : null}
          {step === 4 ? <StepReview project={project} /> : null}
        </CardBody>

        <CardFooter>
          <Link
            href={`/projects/${project.id}`}
            className="mr-auto text-sm text-fg-muted transition-colors hover:text-fg"
          >
            Leave setup
          </Link>
          {step > 0 ? (
            <Button variant="secondary" onClick={() => setStep((index) => index - 1)}>
              <ArrowLeft aria-hidden />
              Back
            </Button>
          ) : null}
          {step < steps.length - 1 ? (
            <Button onClick={next} disabled={save.isPending}>
              {save.isPending ? <Spinner label="Saving" /> : null}
              {readOnlyStep || !patchFor(step, { context, markets, models, gates })
                ? "Continue"
                : "Save and continue"}
              <ArrowRight aria-hidden />
            </Button>
          ) : (
            <Link
              href={`/projects/${project.id}`}
              className="inline-flex h-9 items-center gap-2 rounded-[var(--radius)] border border-border bg-surface-raised px-4 text-sm font-medium text-fg transition-colors hover:bg-surface-hover"
            >
              <Check className="size-4" aria-hidden />
              Done
            </Link>
          )}
        </CardFooter>
      </Card>
    </div>
  );
}

/** What this step owns. Null means the step writes nothing of its own. */
function patchFor(
  step: number,
  draft: {
    context: ProductContext;
    markets: Market[];
    models: ModelRouting;
    gates: Record<string, GateDraft>;
  },
): ProjectPatch | null {
  switch (step) {
    case 0:
      return { product_context: draft.context, markets: draft.markets };
    case 2:
      return { models: draft.models };
    case 3:
      return { approvals: draft.gates };
    default:
      // Sources writes through the credential and CSV endpoints as it goes, and
      // review writes nothing at all.
      return null;
  }
}
