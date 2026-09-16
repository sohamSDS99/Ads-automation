"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Play } from "lucide-react";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { Tooltip } from "@/components/ui/tooltip";
import { ApiError } from "@/lib/api";
import { blockers, launchRun, type ProjectDetail } from "@/lib/api/projects";
import { keys } from "@/lib/queries";

/**
 * Launch a research run.
 *
 * Two refusals, and they are different things. The button is *disabled* when
 * the project is not ready, with the reason in a tooltip — that is knowable
 * before anyone clicks. A lock conflict is not knowable in advance: two people
 * can press this in the same second, so the loser gets a 409 naming the holder
 * and a way to open the run that beat them (PRD §15 NF5d, §16).
 */
export function RunTrigger({
  project,
  size = "md",
}: {
  project: ProjectDetail;
  size?: "sm" | "md";
}) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const stopped = blockers(project);

  const launch = useMutation({
    mutationFn: () => launchRun(project.id),
    onSuccess: async (run) => {
      await queryClient.invalidateQueries({ queryKey: keys.project(project.id) });
      await queryClient.invalidateQueries({ queryKey: keys.projectRuns(project.id) });
      toast.success("Run queued", { description: `${project.name} is now researching.` });
      router.push(`/projects/${project.id}/runs#run-${run.id}`);
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        const holder = (error.problem as { holder?: { run_id?: string } } | null)?.holder;
        toast.error("Already running", {
          description: error.detail,
          action: holder?.run_id
            ? {
                label: "Open it",
                onClick: () =>
                  router.push(`/projects/${project.id}/runs#run-${holder.run_id}`),
              }
            : undefined,
        });
        return;
      }
      toast.error("The run could not start", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      });
    },
  });

  const button = (
    <Button
      size={size}
      onClick={() => launch.mutate()}
      disabled={stopped.length > 0 || launch.isPending}
    >
      {launch.isPending ? <Spinner label="Starting" /> : <Play aria-hidden />}
      Run research
    </Button>
  );

  const [first, ...rest] = stopped;
  if (!first) return button;

  return (
    <Tooltip
      wrapDisabled
      content={
        <span>
          {first.detail}
          {rest.length > 0 ? ` (${rest.length} more to fix)` : ""}
        </span>
      }
    >
      {button}
    </Tooltip>
  );
}
