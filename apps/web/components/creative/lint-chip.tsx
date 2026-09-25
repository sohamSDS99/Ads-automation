import { CircleCheck, CircleDashed, CircleX, TriangleAlert, type LucideIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import type { LintVerdict } from "@/lib/api/creative-runs";
import type { ImageLintVerdict } from "@/lib/api/media-library";
import { cn } from "@/lib/utils";

const VERDICT: Record<
  ImageLintVerdict,
  { label: string; icon: LucideIcon; tone: "neutral" | "warning" | "danger"; ink: string }
> = {
  pass: { label: "Passes lint", icon: CircleCheck, tone: "neutral", ink: "text-status-success" },
  pass_with_warnings: {
    label: "Passes with warnings",
    icon: TriangleAlert,
    tone: "warning",
    ink: "text-status-gate",
  },
  fail: { label: "Fails lint", icon: CircleX, tone: "danger", ink: "text-status-failed" },
  // Image lint only (`image_verdict()`): a rule could not be measured on the file.
  indeterminate: {
    label: "Lint indeterminate",
    icon: CircleDashed,
    tone: "warning",
    ink: "text-status-gate",
  },
};

/**
 * `LintChip` — the linter's verdict: the one an asset was stored with (law 33:
 * nothing leaves `draft` unlinted), or, beside an edit, the one
 * `POST /creative-runs/{id}/lint-preview` returned for the text as typed
 * (`pending` until it has). This only says the verdict, in words and an icon,
 * never by colour alone (§15.2 rule 14); it decides nothing.
 */
export function LintChip({
  verdict,
  pending = false,
}: {
  verdict: LintVerdict | ImageLintVerdict | null;
  pending?: boolean;
}) {
  if (pending) {
    return (
      <Badge className="whitespace-nowrap" data-verdict="pending">
        <CircleDashed className="size-3" aria-hidden />
        Checking
      </Badge>
    );
  }
  if (verdict === null) {
    return (
      <Badge data-verdict="none">
        <CircleDashed className="size-3" aria-hidden />
        Not linted
      </Badge>
    );
  }
  const { label, icon: Icon, tone, ink } = VERDICT[verdict];
  return (
    <Badge tone={tone} data-verdict={verdict} className="whitespace-nowrap">
      <Icon className={cn("size-3", ink)} aria-hidden />
      {label}
    </Badge>
  );
}
