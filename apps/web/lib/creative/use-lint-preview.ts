"use client";

import { useEffect, useState } from "react";

import { ApiError } from "@/lib/api";
import { lintPreview, type CreativeLintTarget, type LintVerdict } from "@/lib/api/creative-runs";
import type { LintResult } from "@/lib/api/lint";

/** §15.4 E: every edit runs a lint preview, debounced 250 ms. */
export const LINT_DEBOUNCE_MS = 250;

export type LintPreviewState = {
  /** The linter's verdict on exactly the text in `target`, or null while it is asked. */
  verdict: LintVerdict | null;
  result: LintResult | null;
  /** True from the keystroke until the answer for that text arrives. */
  pending: boolean;
  error: string | null;
};

/**
 * The pinned linter's verdict on unsaved copy — `POST /creative-runs/{id}/lint-preview`
 * — for the `LintChip` beside an edit. Nothing is decided here: the verdict is
 * the server's, and an answer only counts for the text it was asked about, so
 * a slow reply to an older keystroke can never label newer text.
 *
 * `target` null means nothing is being edited: no request is made.
 */
export function useLintPreview(runId: string, target: CreativeLintTarget | null): LintPreviewState {
  const key = target === null ? null : JSON.stringify(target);
  const [answer, setAnswer] = useState<{ key: string; result: LintResult | null; error: string | null } | null>(
    null,
  );

  useEffect(() => {
    if (key === null) return;
    const asked = JSON.parse(key) as CreativeLintTarget;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      lintPreview(runId, [asked], controller.signal).then(
        (result) => setAnswer({ key, result, error: null }),
        (error: unknown) => {
          if (controller.signal.aborted) return;
          setAnswer({
            key,
            result: null,
            error:
              error instanceof ApiError
                ? error.message
                : "The lint preview could not be reached. Keep typing to try again.",
          });
        },
      );
    }, LINT_DEBOUNCE_MS);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [runId, key]);

  const current = answer !== null && answer.key === key ? answer : null;
  return {
    verdict: current?.result?.verdict ?? null,
    result: current?.result ?? null,
    pending: key !== null && current === null,
    error: current?.error ?? null,
  };
}
