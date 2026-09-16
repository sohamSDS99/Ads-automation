import type { ReactNode, ThHTMLAttributes, TdHTMLAttributes } from "react";

import { cn } from "@/lib/utils";

/**
 * A data table.
 *
 * Wrapped in its own scroll container so a wide table scrolls itself instead of
 * the page, and the header stays put while the body moves.
 */
export function Table({
  children,
  className,
  label,
}: {
  children: ReactNode;
  className?: string;
  /** The table's accessible name — screen readers announce it on entry. */
  label: string;
}) {
  return (
    <div className="w-full overflow-x-auto">
      <table className={cn("w-full min-w-max border-collapse text-sm", className)}>
        <caption className="sr-only">{label}</caption>
        {children}
      </table>
    </div>
  );
}

export function Th({ className, ...props }: ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      scope="col"
      className={cn(
        "border-b px-4 py-2.5 text-left text-xs font-medium tracking-wide text-fg-subtle",
        className,
      )}
      {...props}
    />
  );
}

export function Td({ className, ...props }: TdHTMLAttributes<HTMLTableCellElement>) {
  return <td className={cn("border-b px-4 py-3 align-middle text-fg", className)} {...props} />;
}

export function Tr({ className, children }: { className?: string; children: ReactNode }) {
  return <tr className={cn("hover:bg-surface-hover/60", className)}>{children}</tr>;
}
