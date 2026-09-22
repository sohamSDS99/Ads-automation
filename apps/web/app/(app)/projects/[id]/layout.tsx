import type { ReactNode } from "react";

/**
 * The column every project route sits in.
 *
 * No navigation of its own any more: the pipeline strip that used to run
 * across the top of this is now a group in the side panel
 * (`components/shell/stage-nav.tsx`), which is where the rest of this
 * product's navigation lives.
 *
 * What is left is load-bearing all the same. `flex flex-col` is the column the
 * pages size themselves against — the evidence explorer is a bare
 * `min-h-0 flex-1` child with no other flex parent, and the two run consoles
 * measure their height from here — and `gap-6` is the rhythm between the
 * stacked blocks on the routes that have several.
 */
export default function ProjectLayout({ children }: { children: ReactNode }) {
  return <div className="flex flex-col gap-6">{children}</div>;
}
