import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * A bordered surface.
 *
 * One elevation cue, and it is the border — no shadow on an inline surface
 * (PRD §13.2). Cards group related controls; they are never the page's
 * structure, which is what headings and rules are for.
 */
export function Card({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <section className={cn("rounded-[var(--radius)] border bg-surface-raised", className)}>
      {children}
    </section>
  );
}

export function CardHeader({
  title,
  description,
  actions,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-wrap items-start justify-between gap-3 border-b px-4 py-3.5",
        className,
      )}
    >
      <div className="min-w-0 space-y-1">
        <h2 className="text-[length:var(--text-md)] font-medium tracking-tight text-fg">{title}</h2>
        {description ? (
          <p className="max-w-prose text-sm text-fg-muted">{description}</p>
        ) : null}
      </div>
      {/* `shrink-0` keeps the actions at full size while the title has room
          to give; `max-w-full` and `flex-wrap` are what stop them overflowing
          the card once it does not. Two buttons are ~300px and a 390px
          viewport leaves ~286 inside the card, so without these the primary
          action hangs off the right edge. */}
      {actions ? (
        <div className="flex max-w-full shrink-0 flex-wrap items-center justify-end gap-2">
          {actions}
        </div>
      ) : null}
    </div>
  );
}

export function CardBody({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={cn("px-4 py-4", className)}>{children}</div>;
}

export function CardFooter({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <div className={cn("flex items-center justify-end gap-2 border-t px-4 py-3", className)}>
      {children}
    </div>
  );
}
