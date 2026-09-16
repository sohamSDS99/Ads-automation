"use client";

import { X } from "lucide-react";
import { useId, useState, type KeyboardEvent } from "react";

import { cn } from "@/lib/utils";

/**
 * A short list of short strings — products, differentiators.
 *
 * Enter and comma both commit, because people type lists both ways; Backspace
 * on an empty field removes the last entry, which is the one shortcut anybody
 * tries. Every tag keeps its own remove button so the list is operable without
 * knowing any of that.
 */
export function TagInput({
  label,
  hint,
  values,
  onChange,
  placeholder,
  disabled,
  max = 40,
}: {
  label: string;
  hint?: string;
  values: string[];
  onChange: (values: string[]) => void;
  placeholder?: string;
  disabled?: boolean;
  max?: number;
}) {
  const id = useId();
  const [draft, setDraft] = useState("");

  function commit(raw: string) {
    const value = raw.trim();
    if (!value || values.includes(value) || values.length >= max) {
      setDraft("");
      return;
    }
    onChange([...values, value]);
    setDraft("");
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter" || event.key === ",") {
      event.preventDefault();
      commit(draft);
    } else if (event.key === "Backspace" && !draft && values.length) {
      onChange(values.slice(0, -1));
    }
  }

  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium text-fg">
        {label}
      </label>
      <div
        className={cn(
          "flex min-h-10 w-full flex-wrap items-center gap-1.5 rounded-[var(--radius)] border",
          "bg-surface-raised px-2 py-1.5",
          "focus-within:border-border-strong",
          disabled && "bg-surface",
        )}
      >
        {values.map((value) => (
          <span
            key={value}
            className="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs text-fg"
          >
            {value}
            {disabled ? null : (
              <button
                type="button"
                onClick={() => onChange(values.filter((item) => item !== value))}
                aria-label={`Remove ${value}`}
                className="-mr-0.5 rounded-full p-0.5 text-fg-subtle transition-colors hover:bg-surface-hover hover:text-fg"
              >
                <X className="size-3" aria-hidden />
              </button>
            )}
          </span>
        ))}
        <input
          id={id}
          value={draft}
          disabled={disabled}
          placeholder={values.length ? "" : placeholder}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onKeyDown}
          onBlur={() => commit(draft)}
          className="h-6 min-w-32 flex-1 bg-transparent px-1 text-sm text-fg outline-none placeholder:text-fg-subtle disabled:cursor-not-allowed"
        />
      </div>
      <p className="min-h-4 text-xs text-fg-muted">{hint}</p>
    </div>
  );
}
