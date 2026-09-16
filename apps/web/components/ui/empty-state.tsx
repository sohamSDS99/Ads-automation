import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * Nothing here yet, and what to do about it.
 *
 * An empty state that only says "no results" wastes the one moment the person
 * is definitely looking, so `action` is part of the shape rather than optional
 * decoration around it.
 */
export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  className,
}: {
  icon: LucideIcon;
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-3 rounded-[var(--radius)]",
        "border border-dashed bg-surface px-6 py-14 text-center",
        className,
      )}
    >
      <Icon className="size-6 text-fg-subtle" aria-hidden />
      <h2 className="text-[length:var(--text-md)] font-medium text-fg">{title}</h2>
      {description ? <div className="max-w-md text-sm text-fg-muted">{description}</div> : null}
      {action ? <div className="mt-1">{action}</div> : null}
    </div>
  );
}
