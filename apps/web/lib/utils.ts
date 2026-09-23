import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/**
 * Parse a value into a URL only if it is safely linkable.
 *
 * Returns `null` for anything that is not `http`/`https`, and for anything
 * `URL` refuses. Use this for every `href` built from data — React escapes text
 * but does **not** sanitize `href`, so a `javascript:` value assigned to one
 * executes when somebody clicks it.
 */
export function httpUrl(value: string | null | undefined): URL | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? url : null;
  } catch {
    return null;
  }
}
