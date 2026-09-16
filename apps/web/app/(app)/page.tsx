import { FolderPlus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Can } from "@/lib/session";

export default function ProjectsPage() {
  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-[length:var(--text-xl)] font-semibold tracking-tight">Projects</h1>
          <p className="mt-1 text-sm text-fg-muted">
            A project is one brand, one set of markets, and the evidence sources a research run
            draws on.
          </p>
        </div>
        {/* Hidden for approver and viewer (PRD §13.3); still disabled for
            everyone until the setup wizard lands in P6. */}
        <Can permission="project_write">
          <Button disabled title="Project creation ships in P6">
            <FolderPlus aria-hidden />
            New Project
          </Button>
        </Can>
      </div>

      <div className="flex flex-col items-center justify-center gap-3 rounded-[var(--radius)] border border-dashed bg-surface px-6 py-16 text-center">
        <FolderPlus className="size-6 text-fg-subtle" aria-hidden />
        <h2 className="text-[length:var(--text-md)] font-medium">No projects yet</h2>
        <p className="max-w-md text-sm text-fg-muted">
          Nothing has been created in this workspace. Project setup arrives with the wizard in
          Phase P6.
        </p>
      </div>
    </div>
  );
}
