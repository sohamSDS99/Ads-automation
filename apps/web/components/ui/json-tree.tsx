"use client";

import { ChevronRight } from "lucide-react";
import { useState } from "react";

import { cn } from "@/lib/utils";

/**
 * A node's output, as a tree you can walk rather than a wall you scroll.
 *
 * Containers collapse; leaves render by type so a number does not look like the
 * string of the same digits. Depth 0 and 1 start open, because the first useful
 * question is always "what did it produce", and the second is "how much of it".
 */
const OPEN_TO_DEPTH = 1;

export function JsonTree({ value, className }: { value: unknown; className?: string }) {
  // The root's own row would read `{4}` and put everything worth seeing behind
  // a disclosure triangle, so the top level renders its children directly.
  const entries = containerEntries(value);
  return (
    <div className={cn("font-mono text-xs leading-relaxed", className)}>
      {entries === null ? (
        <Node label={null} value={value} depth={0} />
      ) : (
        entries.map(([key, child]) => <Node key={key} label={key} value={child} depth={0} />)
      )}
    </div>
  );
}

function Node({ label, value, depth }: { label: string | null; value: unknown; depth: number }) {
  const [open, setOpen] = useState(depth <= OPEN_TO_DEPTH);
  const entries = containerEntries(value);

  if (entries === null) {
    return (
      <div className="flex gap-1.5 py-0.5">
        {label === null ? null : <Key name={label} />}
        <Leaf value={value} />
      </div>
    );
  }

  const isArray = Array.isArray(value);
  const summary = `${isArray ? "[" : "{"}${entries.length}${isArray ? "]" : "}"}`;

  return (
    <div className="py-0.5">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex min-h-6 w-full items-center gap-1 rounded-[calc(var(--radius)-6px)] text-left hover:bg-surface-hover"
      >
        <ChevronRight
          aria-hidden
          className={cn("size-3 shrink-0 text-fg-subtle transition-transform", open && "rotate-90")}
        />
        {label === null ? null : <Key name={label} />}
        <span className="text-fg-subtle">{summary}</span>
      </button>
      {open ? (
        <div className="ml-3 border-l pl-2">
          {entries.map(([key, child]) => (
            <Node key={key} label={key} value={child} depth={depth + 1} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function Key({ name }: { name: string }) {
  return <span className="shrink-0 text-fg-muted">{name}:</span>;
}

function Leaf({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span className="text-fg-subtle">null</span>;
  if (typeof value === "boolean" || typeof value === "number") {
    return <span className="text-accent">{String(value)}</span>;
  }
  const text = String(value);
  if (isUrl(text)) {
    return (
      <a
        href={text}
        target="_blank"
        rel="noreferrer"
        className="break-all text-accent underline underline-offset-2"
      >
        {text}
      </a>
    );
  }
  return <span className="break-words whitespace-pre-wrap text-fg">{text}</span>;
}

function containerEntries(value: unknown): [string, unknown][] | null {
  if (Array.isArray(value)) return value.map((item, index) => [String(index), item]);
  if (value !== null && typeof value === "object") return Object.entries(value);
  return null;
}

function isUrl(text: string): boolean {
  return text.startsWith("https://") || text.startsWith("http://");
}
