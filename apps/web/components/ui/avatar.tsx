import { cn } from "@/lib/utils";

/**
 * Initials in a circle.
 *
 * No photographs anywhere in this product, so there is no image to fall back
 * from — which makes the colour the only thing distinguishing two people. It is
 * derived from the name so the same person is the same colour on every screen,
 * and every hue in the set clears contrast against its own foreground.
 */
const HUES = [
  "bg-[#e0e7ff] text-[#3730a3] dark:bg-[#312e81] dark:text-[#c7d2fe]",
  "bg-[#d1fae5] text-[#065f46] dark:bg-[#064e3b] dark:text-[#a7f3d0]",
  "bg-[#fee2e2] text-[#991b1b] dark:bg-[#7f1d1d] dark:text-[#fecaca]",
  "bg-[#fef3c7] text-[#92400e] dark:bg-[#78350f] dark:text-[#fde68a]",
  "bg-[#e9d5ff] text-[#6b21a8] dark:bg-[#581c87] dark:text-[#e9d5ff]",
  "bg-[#cffafe] text-[#155e75] dark:bg-[#164e63] dark:text-[#a5f3fc]",
];

export function initialsOf(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0]!.slice(0, 2).toUpperCase();
  return (parts[0]![0]! + parts.at(-1)![0]!).toUpperCase();
}

function hueOf(name: string): string {
  let sum = 0;
  for (const character of name) sum = (sum + character.codePointAt(0)!) % 997;
  return HUES[sum % HUES.length]!;
}

export function Avatar({
  name,
  size = "md",
  className,
}: {
  name: string;
  size?: "sm" | "md";
  className?: string;
}) {
  return (
    <span
      aria-hidden
      className={cn(
        "inline-flex shrink-0 items-center justify-center rounded-full font-medium",
        size === "sm" ? "size-6 text-[0.625rem]" : "size-8 text-xs",
        hueOf(name),
        className,
      )}
    >
      {initialsOf(name)}
    </span>
  );
}
