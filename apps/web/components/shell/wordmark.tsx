import { cn } from "@/lib/utils";

/**
 * The product mark. The same dot-and-name as the sidebar, so the sign-in page
 * and the app read as one place rather than two.
 */
export function Wordmark({ className, size = "md" }: { className?: string; size?: "md" | "lg" }) {
  return (
    <span className={cn("inline-flex items-center gap-2", className)}>
      <span
        aria-hidden
        className={cn("shrink-0 rounded-full bg-accent", size === "lg" ? "size-2.5" : "size-2")}
      />
      <span
        className={cn(
          "font-semibold tracking-tight text-fg",
          size === "lg" ? "text-[length:var(--text-md)]" : "text-sm",
        )}
      >
        Research Agent
      </span>
    </span>
  );
}
