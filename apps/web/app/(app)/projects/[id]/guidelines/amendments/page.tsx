"use client";

import { useParams } from "next/navigation";

import { AmendmentInbox } from "@/components/amendments/inbox";
import { SignOffMatrixEditor } from "@/components/governance/signoff-matrix-editor";

/**
 * `/projects/[id]/guidelines/amendments` — governance for this project.
 *
 * The amendment inbox and the sign-off matrix share a page because they are two
 * halves of one question: what changes the rulebook, and who answers for it.
 * Splitting them put the matrix on a settings screen nobody visited, which is
 * how a project ends up with a legal owner who left the company.
 */
export default function AmendmentsPage() {
  const params = useParams<{ id: string }>();

  return (
    <div className="flex flex-col gap-8">
      <section className="flex flex-col gap-4">
        <header>
          <h1 className="text-[length:var(--text-xl)] font-semibold tracking-tight">
            Amendments
          </h1>
          <p className="mt-1 max-w-prose text-sm text-fg-muted">
            Reasons the published rulebook should change — a policy page that moved, a claim
            that expired, an ad Google disapproved. Mechanical changes apply themselves; the
            rest wait for a person.
          </p>
        </header>
        <AmendmentInbox projectId={params.id} />
      </section>

      <SignOffMatrixEditor projectId={params.id} />
    </div>
  );
}
