"use client";

import { ArrowLeftRight } from "lucide-react";
import { useId, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ApiError } from "@/lib/api";
import type { CreativeAssetItem, LintVerdict } from "@/lib/api/creative-runs";
import { charCount } from "@/lib/creative/char-count";
import { useLintPreview } from "@/lib/creative/use-lint-preview";
import { useEditCreativeAsset } from "@/lib/queries";
import { cn } from "@/lib/utils";

/** Where the ad runs — what the linter needs beside the text (a `LintTarget`'s scope). */
export type LintScope = { campaign_type: string; market: string; language: string };

export type CopyEdit = {
  /** What the cell shows: the draft while editing, the stored text otherwise. */
  text: string;
  dirty: boolean;
  count: number;
  /** The linter's verdict on `text`: the preview's for a draft, the stored one otherwise. */
  verdict: LintVerdict | null;
  pending: boolean;
  error: string | null;
  saving: boolean;
  change: (text: string) => void;
  save: () => Promise<void>;
  revert: () => void;
};

/**
 * One headline or description being rewritten in place (§15.4 E). The count
 * moves with every keystroke — display — and the chip follows the pinned
 * linter through the debounced preview. Saving is `PATCH /creative-assets/{id}`:
 * the server runs the node's checks and the pin and stores only a pass, with
 * `lineage.origin = 'human_edit'`; a refusal keeps the draft and says why.
 */
export function useCopyEdit(runId: string, asset: CreativeAssetItem, scope: LintScope): CopyEdit {
  const saved = asset.text ?? "";
  const [draft, setDraft] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const edit = useEditCreativeAsset(runId);
  const text = draft ?? saved;
  const dirty = draft !== null && draft !== saved;
  const lint = useLintPreview(
    runId,
    dirty
      ? {
          ref: asset.id,
          surface: asset.surface,
          ...scope,
          text,
          generated_by_ai: asset.generated_by_ai,
        }
      : null,
  );

  return {
    text,
    dirty,
    count: charCount(asset.surface, text),
    verdict: dirty ? lint.verdict : asset.lint_verdict,
    pending: dirty && lint.pending,
    error: error ?? (dirty ? lint.error : null),
    saving: edit.isPending,
    change: (next) => {
      setDraft(next);
      setError(null);
    },
    save: async () => {
      if (!dirty || edit.isPending) return;
      try {
        await edit.mutateAsync({ assetId: asset.id, text });
        setDraft(null);
        setError(null);
      } catch (failure) {
        setError(
          failure instanceof ApiError
            ? failure.detail
            : "The edit could not be saved. Check your connection, then press Enter to try again.",
        );
      }
    },
    revert: () => {
      setDraft(null);
      setError(null);
    },
  };
}

/**
 * The text cell. Enter saves, Escape puts the stored text back, leaving the
 * cell saves. For anyone who cannot edit, or once the asset is frozen at
 * release, the text is plain text — the control is absent, not disabled
 * (§15.5 item 4).
 */
export function CopyText({
  id,
  label,
  edit,
  editable,
  multiline = false,
  below,
  children,
}: {
  id: string;
  label: string;
  edit: CopyEdit;
  editable: boolean;
  /** A description wraps as it will on the page; a headline is one line. */
  multiline?: boolean;
  /** Shown under the text either way: claim chips, a counter. */
  below?: React.ReactNode;
  /** The read-only rendering (a claim span highlighted, say). */
  children?: React.ReactNode;
}) {
  const errorId = useId();
  if (!editable) {
    return (
      <div className="flex flex-col gap-1">
        <span className="block whitespace-normal text-sm text-fg">{children ?? edit.text}</span>
        {below}
      </div>
    );
  }
  const field = {
    id,
    "aria-label": label,
    value: edit.text,
    "aria-invalid": edit.verdict === "fail" ? true : undefined,
    "aria-describedby": edit.error ? errorId : undefined,
    spellCheck: true,
    onBlur: () => void edit.save(),
    className: cn(
      "w-full rounded-token border border-transparent bg-transparent px-2 py-1 text-sm text-fg",
      "hover:border-border focus:border-border-strong focus:bg-surface-raised",
      "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
      edit.dirty && "border-border bg-surface-raised",
      multiline && "field-sizing-content resize-none",
    ),
  };
  // Enter saves in both: an ad's copy is one line, however it wraps on screen.
  const keys = (event: React.KeyboardEvent) => {
    if (event.key === "Enter") {
      event.preventDefault();
      void edit.save();
    } else if (event.key === "Escape" && edit.dirty) {
      event.preventDefault();
      edit.revert();
    }
  };
  return (
    <div className="flex flex-col gap-1">
      {multiline ? (
        <textarea {...field} rows={2} onChange={(event) => edit.change(event.target.value)} onKeyDown={keys} />
      ) : (
        <input {...field} type="text" onChange={(event) => edit.change(event.target.value)} onKeyDown={keys} />
      )}
      {below ? <div className="px-2">{below}</div> : null}
      {edit.error ? (
        <p id={errorId} role="alert" className="max-w-md whitespace-normal px-2 text-xs text-status-failed-ink">
          {edit.error}
        </p>
      ) : edit.saving ? (
        <p className="px-2 text-xs text-fg-muted">Saving and re-linting at the run’s pin…</p>
      ) : null}
    </div>
  );
}

/**
 * "Swap in" on a reserve: a menu of what the ad carries, the same category
 * first. Keyboard: Enter or Space opens it, the arrows move, Enter swaps,
 * Escape closes (Radix menu semantics).
 */
export function SwapMenu({
  reserve,
  carried,
  labelOf,
  categoryOf,
  onSwap,
  busy,
}: {
  reserve: CreativeAssetItem;
  carried: CreativeAssetItem[];
  labelOf: (asset: CreativeAssetItem) => string;
  categoryOf?: (asset: CreativeAssetItem) => string | null;
  onSwap: (out: CreativeAssetItem) => void;
  busy: boolean;
}) {
  const category = categoryOf?.(reserve) ?? null;
  const ordered = category
    ? [...carried].sort((a, b) => Number(categoryOf?.(b) === category) - Number(categoryOf?.(a) === category))
    : carried;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="secondary"
          size="sm"
          disabled={busy}
          aria-label={`Swap in “${reserve.text ?? ""}”`}
          data-testid={`swap-${reserve.id}`}
        >
          <ArrowLeftRight aria-hidden />
          Swap in
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent className="max-h-96 max-w-md overflow-y-auto">
        <DropdownMenuLabel>Replace in the ad</DropdownMenuLabel>
        {ordered.map((asset) => (
          <DropdownMenuItem key={asset.id} onSelect={() => onSwap(asset)}>
            <span className="w-8 shrink-0 font-mono text-xs tabular-nums text-fg-muted">{labelOf(asset)}</span>
            <span className="min-w-0 flex-1 truncate">{asset.text}</span>
            {categoryOf?.(asset) ? (
              <span className="shrink-0 text-xs text-fg-muted">{categoryOf(asset)}</span>
            ) : null}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
