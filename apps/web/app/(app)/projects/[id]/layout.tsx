import type { ReactNode } from "react";

import { StageTabs } from "@/components/plan/stage-tabs";

/**
 * Every project route sits under the pipeline strip (Stage 02 PRD §15.1).
 *
 * A server component with no data of its own: the strip needs the project id,
 * which is in the URL, and nothing else. Putting it here rather than in each
 * page is what stops the tabs disappearing on the one route somebody forgets.
 *
 * The strip is full-bleed — it cancels `<main>`'s padding so its rule spans
 * the content area — and its labels sit on the content gutter rather than on
 * whatever max-width the page below happens to use. Project routes are 4xl,
 * 5xl and full-width depending on what they hold, so a strip that tried to
 * line up with one of them would be visibly out by 60px on the others.
 */
export default async function ProjectLayout({
  children,
  params,
}: {
  children: ReactNode;
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return (
    <div className="flex flex-col gap-6">
      <div className="-mx-6 -mt-6 border-b">
        <div className="px-6">
          <StageTabs projectId={id} />
        </div>
      </div>
      {children}
    </div>
  );
}
