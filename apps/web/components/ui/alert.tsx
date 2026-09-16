import { CircleAlert, Info, TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

const TONES = {
  info: { icon: Info, text: "text-fg", border: "border-border-strong" },
  warning: { icon: TriangleAlert, text: "text-fg", border: "border-status-gate" },
  error: { icon: CircleAlert, text: "text-fg", border: "border-status-failed" },
} as const;

export function Alert({
  tone = "info",
  title,
  children,
  className,
}: {
  tone?: keyof typeof TONES;
  title?: string;
  children?: ReactNode;
  className?: string;
}) {
  const { icon: Icon, text, border } = TONES[tone];
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      className={cn(
        "flex gap-3 rounded-[var(--radius)] border bg-surface px-3.5 py-3 text-sm",
        border,
        text,
        className,
      )}
    >
      <Icon
        aria-hidden
        className={cn(
          "mt-0.5 size-4 shrink-0",
          tone === "error" && "text-status-failed",
          tone === "warning" && "text-status-gate",
          tone === "info" && "text-fg-subtle",
        )}
      />
      <div className="min-w-0 space-y-1">
        {title ? <p className="font-medium">{title}</p> : null}
        {children ? <div className="text-fg-muted">{children}</div> : null}
      </div>
    </div>
  );
}
