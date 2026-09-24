import { CircleCheck, CircleDashed, CircleX, TriangleAlert, type LucideIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import type { LintVerdict } from "@/lib/api/creative-runs";
import { cn } from "@/lib/utils";

const VERDICT: Record<
  LintVerdict,
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
};

/**
 * `LintChip` — the verdict an asset was stored with (law 33: nothing leaves
 * `draft` unlinted). The verdict is the linter's, carried on the asset; this
 * only says it, in words and an icon, never by colour alone (§15.2 rule 14).
 */
export function LintChip({ verdict }: { verdict: LintVerdict | null }) {
  if (verdict === null) {
    return (
      <Badge>
        <CircleDashed className="size-3" aria-hidden />
        Not linted
      </Badge>
    );
  }
  const { label, icon: Icon, tone, ink } = VERDICT[verdict];
  return (
    <Badge tone={tone}>
      <Icon className={cn("size-3", ink)} aria-hidden />
      {label}
    </Badge>
  );
}
