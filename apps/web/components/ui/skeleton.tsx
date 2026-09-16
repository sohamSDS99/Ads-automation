import { cn } from "@/lib/utils";

/**
 * A loading placeholder shaped like the thing it stands in for.
 *
 * Deliberately a slow pulse rather than a sweeping shimmer: this is a tool
 * people wait in, and a fast moving highlight in the corner of the eye is
 * noise. Stops entirely under `prefers-reduced-motion`.
 */
export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      aria-hidden
      className={cn(
        "animate-pulse rounded-[calc(var(--radius)-4px)] bg-surface-hover motion-reduce:animate-none",
        className,
      )}
    />
  );
}
