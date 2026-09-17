"use client";

import { useEffect, useState } from "react";

import { cn } from "@/lib/utils";

/**
 * The report's table of contents, and where the reader is in it.
 *
 * Tracked with an IntersectionObserver rather than a scroll handler: the
 * observer fires only when a section boundary actually crosses the viewport, so
 * a long report costs nothing to read.
 */
export type TocEntry = { id: string; label: string };

export function Toc({ entries, className }: { entries: TocEntry[]; className?: string }) {
  const active = useActiveSection(entries.map((entry) => entry.id));

  return (
    <nav aria-label="Report contents" className={cn("text-sm", className)}>
      <ul className="space-y-0.5 border-l">
        {entries.map((entry) => (
          <li key={entry.id}>
            <a
              href={`#${entry.id}`}
              aria-current={entry.id === active ? "true" : undefined}
              className={cn(
                "-ml-px block border-l py-1 pl-3 transition-colors",
                entry.id === active
                  ? "border-accent font-medium text-fg"
                  : "border-transparent text-fg-muted hover:border-border-strong hover:text-fg",
              )}
            >
              {entry.label}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}

function useActiveSection(ids: string[]): string | null {
  const [active, setActive] = useState<string | null>(ids[0] ?? null);
  const key = ids.join("|");

  useEffect(() => {
    const sections = key
      .split("|")
      .map((id) => document.getElementById(id))
      .filter((element): element is HTMLElement => element !== null);
    if (sections.length === 0) return;

    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
        if (visible) setActive(visible.target.id);
      },
      // The band is the top third of the viewport: a heading counts as "where
      // I am" once it reaches reading position, not when it first appears.
      { rootMargin: "-80px 0px -66% 0px", threshold: 0 },
    );
    for (const section of sections) observer.observe(section);
    return () => observer.disconnect();
  }, [key]);

  return active;
}
