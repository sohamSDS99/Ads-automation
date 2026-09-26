"use client";

import { Check, Keyboard, RotateCcw, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { MonoId } from "@/components/creative/mono-id";
import { CheckerDefs } from "@/components/creative/media-frame";
import { ReferenceCompare, ZoomControls, ZoomedImage, type ReferenceImage } from "@/components/creative/reference-compare";
import { AiGeneratedChip } from "@/components/creative/rendition-grid";
import { ReviewChecklist } from "@/components/creative/review-checklist";
import { ReviewFilmstrip } from "@/components/creative/review-filmstrip";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Popover } from "@/components/ui/popover";
import { Spinner } from "@/components/ui/spinner";
import { Textarea } from "@/components/ui/textarea";
import { ApiError } from "@/lib/api";
import { decideApproval, type ApprovalDecision, type ApprovalItem } from "@/lib/api/approvals";
import { mediaContentUrl, mediaReferenceContentUrl, type MediaReference } from "@/lib/api/media-library";
import { reviewCard, saveReviewDraft, type ReviewDecision, type ReviewGate, type ReviewItem } from "@/lib/api/review";
import { absoluteTime } from "@/lib/format";
import {
  CHECKS,
  canApprove,
  clearDecision,
  decide,
  DECISION_LABEL,
  initialCursor,
  initialState,
  nextUndecided,
  recordedState,
  sumUsd,
  tally,
  tallyLine,
  ticked,
  toDraft,
  toggleCheck,
  toSubmission,
  usd,
  type CheckKey,
  type ReviewState,
} from "@/lib/creative/review";
import { FIT, zoomIn, zoomOut, type Zoom } from "@/lib/creative/zoom";
import { useRegenerationEstimates } from "@/lib/queries";
import { cn } from "@/lib/utils";

/** Autosave waits this long after the last change, so a run of ticks is one PUT. */
const AUTOSAVE_MS = 600;

type Save =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "saving" }
  | { kind: "saved"; at: string }
  | { kind: "failed"; detail: string };

function shortcuts(gate: ReviewGate, deciding: boolean): [keys: string, what: string][] {
  const rows: [string, string][] = [
    ["J / K", "Next / previous asset"],
    ["N", "Next undecided asset"],
    ["[ / ]", "Previous / next rendition"],
    ["= / - / 0", "Zoom in / out / fit, in every view at once"],
    ["Arrow keys", "On a zoomed view: pan"],
  ];
  if (deciding) {
    rows.push(
      ["1 2 3 4", "Tick or untick a check"],
      ["A", "Approve — only once all four checks are ticked"],
      ["R", "Reject"],
    );
    if (gate === "G8") rows.push(["G", "Regenerate — opens a note"]);
    rows.push(["Backspace", "Clear the decision on this asset"], ["⌘ Enter / Ctrl Enter", "Review and record every decision"]);
  }
  rows.push(["?", "Show these shortcuts"]);
  return rows;
}

/** A text field has the keyboard; a checkbox or a button does not. */
function typing(target: EventTarget | null): boolean {
  const node = target as HTMLElement | null;
  if (!node?.closest) return false;
  return Boolean(
    node.closest(
      'textarea, select, [contenteditable="true"], input:not([type="checkbox"]):not([type="radio"]):not([type="button"])',
    ),
  );
}

function ratioOfPx(px: string): number {
  const [w, h] = px.split(/[x×]/).map(Number);
  return w && h ? w / h : 1;
}

function kindNoun(item: ReviewItem): string {
  return item.kind === "video" ? "video" : "image";
}

/**
 * The G8 / G8b review workspace (Stage 04 PRD §15.4 H), keyboard first.
 *
 * One asset at a time, full-bleed in the centre. Left, the queue with every
 * asset's decision state. Right, `ReferenceCompare` at the stage's own zoom,
 * the four-item `ReviewChecklist` and VISION's notes under a plain
 * `Advisory` label. `A` approve, `R` reject, `G` regenerate (a note), `J`/`K`
 * move, `1`–`4` tick. Every change autosaves to `draft_state`; nothing is a
 * decision until the reviewer records the whole card, after seeing the tally
 * and what the regenerations cost. G8b is the same, without Regenerate.
 *
 * Decide controls exist only for someone the server says may decide
 * (`can_decide`) while the gate is pending: for anyone else they are absent,
 * not disabled, and the workspace reads `Awaiting {decider}` (§15.5 item 4).
 */
export function ReviewWorkspace({
  approval,
  references,
  referencesError,
  models,
  onRecorded,
}: {
  approval: ApprovalItem;
  references: MediaReference[];
  referencesError: string | null;
  /** asset id → the media model that made it, for the `AI-generated` chip. */
  models: ReadonlyMap<string, string>;
  onRecorded: (result: ApprovalDecision) => void;
}) {
  const gate = approval.gate_key as ReviewGate;
  const card = useMemo(() => reviewCard(approval), [approval]);
  const items = card.items;
  const order = useMemo(() => items.map((item) => item.asset_id), [items]);
  const deciding = approval.status === "pending" && approval.can_decide;
  const draft = deciding ? (approval.draft_state as Parameters<typeof initialState>[1]) : null;

  const [state, setState] = useState<ReviewState>(() =>
    deciding ? initialState(items, draft, gate) : recordedState(items),
  );
  const [cursor, setCursor] = useState(() => (deciding ? initialCursor(items, draft) : 0));
  const [rendition, setRendition] = useState(0);
  const [zoom, setZoom] = useState<Zoom>(FIT);
  const [noteOpen, setNoteOpen] = useState(false);
  const [submitOpen, setSubmitOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const [refusal, setRefusal] = useState<string | null>(null);
  const [save, setSave] = useState<Save>(
    draft && typeof draft === "object" && "saved_at" in draft && typeof draft.saved_at === "string"
      ? { kind: "saved", at: draft.saved_at }
      : { kind: "idle" },
  );

  const item = items[cursor] ?? null;
  const current = item ? (state[item.asset_id] ?? null) : null;
  const shown = item?.renditions[Math.min(rendition, item.renditions.length - 1)] ?? null;
  const counts = tally(state, order);

  // --- moving ----------------------------------------------------------------
  const go = useCallback(
    (index: number) => {
      const next = Math.max(0, Math.min(items.length - 1, index));
      setCursor(next);
      setRendition(0);
      setZoom(FIT);
    },
    [items.length],
  );

  const say = (text: string) => {
    // Re-announce an identical sentence: clear first, then set.
    setAnnouncement("");
    window.setTimeout(() => setAnnouncement(text), 30);
  };

  // --- deciding --------------------------------------------------------------
  const apply = (decision: ReviewDecision, note?: string) => {
    if (!deciding || !item || !current) return;
    const outcome = decide(current, decision, gate, note);
    if ("refused" in outcome) {
      if (outcome.refused === "needs_ticks") {
        say(`Approve needs all four checks. ${ticked(current.checklist)} of 4 ticked — press 1 to 4.`);
      } else if (outcome.refused === "needs_note") {
        say("A regeneration needs a note saying what should change.");
      }
      return;
    }
    const nextState = { ...state, [item.asset_id]: outcome.next };
    setState(nextState);
    const after = nextUndecided(nextState, order, cursor);
    const decided = `${DECISION_LABEL[decision]} ${kindNoun(item)} ${cursor + 1} of ${items.length}.`;
    if (after === null) {
      say(`${decided} Every asset is decided — press Command or Control Enter to review and record.`);
    } else {
      go(after);
      say(`${decided} Now ${after + 1} of ${items.length}.`);
    }
  };

  const toggle = (key: CheckKey) => {
    if (!deciding || !item || !current) return;
    const { next, withdrew } = toggleCheck(current, key);
    setState((prior) => ({ ...prior, [item.asset_id]: next }));
    const label = CHECKS.find((check) => check.key === key)?.label ?? key;
    say(
      withdrew
        ? `${label} unticked. The approval is withdrawn: an approval needs all four checks.`
        : `${label} ${next.checklist[key] ? "ticked" : "unticked"}. ${ticked(next.checklist)} of 4.`,
    );
  };

  const clear = () => {
    if (!deciding || !item || !current || current.decision === null) return;
    setState((prior) => ({ ...prior, [item.asset_id]: clearDecision(current) }));
    say(`Decision cleared on ${kindNoun(item)} ${cursor + 1}.`);
  };

  // --- autosave --------------------------------------------------------------
  const latest = useRef({ state, cursor });
  latest.current = { state, cursor };
  const dirty = useRef(false);
  const chain = useRef<Promise<unknown>>(Promise.resolve());
  const flush = useCallback(() => {
    if (!dirty.current) return;
    dirty.current = false;
    const { state: now, cursor: at } = latest.current;
    const body = toDraft(now, order, order[at] ?? null);
    setSave({ kind: "saving" });
    chain.current = chain.current
      .then(() => saveReviewDraft(approval.id, body))
      .then((saved) => {
        const at = typeof saved.draft_state.saved_at === "string" ? saved.draft_state.saved_at : new Date().toISOString();
        if (!dirty.current) setSave({ kind: "saved", at });
      })
      .catch((caught: unknown) => {
        dirty.current = true;
        setSave({
          kind: "failed",
          detail: caught instanceof ApiError ? caught.detail : "The draft could not be saved",
        });
      });
  }, [approval.id, order]);

  const mounted = useRef(false);
  useEffect(() => {
    if (!deciding) return;
    if (!mounted.current) {
      mounted.current = true;
      return;
    }
    dirty.current = true;
    setSave({ kind: "pending" });
    const timer = window.setTimeout(flush, AUTOSAVE_MS);
    return () => window.clearTimeout(timer);
  }, [state, cursor, deciding, flush]);
  // A route change mid-review still saves what was there.
  useEffect(() => () => flush(), [flush]);

  // --- the cost of the regenerations -----------------------------------------
  const regenerating = useMemo(
    () => (gate === "G8" ? order.filter((id) => state[id]?.decision === "regenerate") : []),
    [gate, order, state],
  );
  const estimates = useRegenerationEstimates(regenerating);
  const priced = estimates.every((query) => query.isSuccess);
  const regenerateUsd = priced ? sumUsd(estimates.map((query) => query.data?.estimate_usd ?? "0")) : null;
  const priceError = estimates.find((query) => query.isError)?.error;
  const media = estimates.find((query) => query.data)?.data ?? null;

  // --- recording -------------------------------------------------------------
  const [recording, setRecording] = useState(false);
  const record = async () => {
    const submission = toSubmission(state, order);
    if ("undecided" in submission) return;
    setRecording(true);
    setRefusal(null);
    try {
      const result = await decideApproval(approval.id, {
        decision: "approve",
        edited_proposal: { items: submission.items },
      });
      dirty.current = false;
      setSubmitOpen(false);
      onRecorded(result);
    } catch (caught) {
      const problem = caught instanceof ApiError ? caught : null;
      const assetId = problem?.problem && typeof (problem.problem as Record<string, unknown>)["asset_id"] === "string"
        ? ((problem.problem as Record<string, unknown>)["asset_id"] as string)
        : null;
      const at = assetId ? order.indexOf(assetId) : -1;
      if (at >= 0) go(at);
      setSubmitOpen(false);
      setRefusal(
        `${problem?.detail ?? "The decisions were not recorded."}${at >= 0 ? ` Asset ${at + 1} is open.` : ""} Nothing was recorded; your decisions are still here.`,
      );
    } finally {
      setRecording(false);
    }
  };

  const openSubmit = () => {
    if (!deciding) return;
    if (counts.undecided > 0) {
      say(`${counts.undecided} undecided. Press N to go to the next one.`);
      return;
    }
    setSubmitOpen(true);
  };

  // --- keys ------------------------------------------------------------------
  const onKey = useRef<(event: KeyboardEvent) => void>(() => {});
  onKey.current = (event: KeyboardEvent) => {
    if (event.defaultPrevented || noteOpen || submitOpen) return;
    if (event.altKey) return;
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault();
      openSubmit();
      return;
    }
    if (event.metaKey || event.ctrlKey || typing(event.target)) return;
    const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
    const act = (fn: () => void) => {
      event.preventDefault();
      fn();
    };
    switch (key) {
      case "?":
        return act(() => setShortcutsOpen((open) => !open));
      case "j":
        return act(() => go(cursor + 1));
      case "k":
        return act(() => go(cursor - 1));
      case "n":
        return act(() => {
          const next = nextUndecided(state, order, cursor);
          if (next === null) say("Every asset is decided.");
          else go(next);
        });
      case "[":
        return act(() => {
          setRendition((at) => Math.max(0, at - 1));
          setZoom(FIT);
        });
      case "]":
        return act(() => {
          setRendition((at) => Math.min((item?.renditions.length ?? 1) - 1, at + 1));
          setZoom(FIT);
        });
      case "=":
      case "+":
        return act(() => setZoom((z) => zoomIn(z)));
      case "-":
        return act(() => setZoom((z) => zoomOut(z)));
      case "0":
        return act(() => setZoom(FIT));
    }
    if (!deciding) return;
    switch (key) {
      case "a":
        return act(() => apply("approve"));
      case "r":
        return act(() => apply("reject"));
      case "g":
        if (gate === "G8") act(() => setNoteOpen(true));
        return;
      case "Backspace":
        return act(clear);
      case "1":
      case "2":
      case "3":
      case "4": {
        const check = CHECKS[Number(key) - 1];
        if (check) act(() => toggle(check.key));
        return;
      }
    }
  };
  useEffect(() => {
    const listener = (event: KeyboardEvent) => onKey.current(event);
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, []);

  // --- what the right column compares with ------------------------------------
  const compared = useMemo((): { refs: ReferenceImage[]; reason: string | null } => {
    if (!item) return { refs: [], reason: null };
    if (item.kind === "video") {
      return {
        refs: [],
        reason: "No product reference goes to a video model, so there is nothing to compare this video with.",
      };
    }
    if (item.product_refs.length === 0) {
      return { refs: [], reason: "This image was made without a product reference, so there is nothing to compare it with." };
    }
    if (referencesError) return { refs: [], reason: `The project's references could not be read: ${referencesError}` };
    // A reference uploaded after the gate opened cannot be what the model was given.
    const refs = references
      .filter(
        (ref) =>
          ref.kind === "product_reference" &&
          ref.product_ref !== null &&
          item.product_refs.includes(ref.product_ref) &&
          ref.created_at <= approval.created_at,
      )
      .map((ref) => ({
        id: ref.id,
        src: mediaReferenceContentUrl(ref.id),
        ratio: ref.width / ref.height,
        label: ref.product_ref ?? "reference",
      }));
    return {
      refs,
      reason:
        refs.length === 0
          ? `Reference ${item.product_refs.join(", ")} is not among this project's references, so it cannot be shown.`
          : null,
    };
  }, [item, references, referencesError, approval.created_at]);

  if (!item || !current) return null;
  const noun = kindNoun(item);
  const stageSrc = shown ? mediaContentUrl(shown.media_id, item.kind === "video" ? "preview" : "master") : null;
  const stageRatio = shown ? ratioOfPx(shown.px) : 1;
  const assetLabel = `${item.campaign_ref} · ${item.concept_id}`;
  const allDecided = counts.undecided === 0;
  const awaiting = approval.assignee_email ?? "the brand owner";
  const remainingAfter = media && regenerateUsd !== null ? Number(media.remaining_usd) - regenerateUsd : null;

  return (
    <div className="flex flex-col gap-3 lg:h-full lg:min-h-0">
      <CheckerDefs />
      <p aria-live="polite" className="sr-only">
        {announcement}
      </p>

      <div className="flex min-h-0 flex-col gap-3 lg:flex-1 lg:flex-row">
        <div className="lg:w-52 lg:shrink-0">
          <ReviewFilmstrip
            items={items}
            state={state}
            cursor={cursor}
            onSelect={go}
            label={(it) => `${it.campaign_ref} · ${it.concept_id}`}
          />
        </div>

        {/* The stage: the asset, full-bleed, at its true ratio. */}
        <section aria-label={`${noun} ${cursor + 1} of ${items.length}`} className="flex min-h-0 min-w-0 flex-1 flex-col gap-2">
          <header className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1.5">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <h2 className="text-base font-medium tabular-nums text-fg">
                {cursor + 1} of {items.length}
              </h2>
              <span className="truncate text-sm text-fg-muted">
                {assetLabel} · {noun}
              </span>
              <AiGeneratedChip model={models.get(item.asset_id) ?? null} />
              {item.regenerated_from ? (
                <span className="flex items-center gap-1 text-xs text-fg-muted">
                  Regenerated from <MonoId value={item.regenerated_from} label="Copy the original asset id" />
                </span>
              ) : null}
            </div>
            <div className="flex items-center gap-1">
              {item.kind === "image" ? <ZoomControls zoom={zoom} onZoom={setZoom} /> : null}
              <Popover
                open={shortcutsOpen}
                onOpenChange={setShortcutsOpen}
                side="bottom"
                align="end"
                trigger={
                  <Button variant="ghost" size="sm" aria-keyshortcuts="?">
                    <Keyboard aria-hidden />
                    Keyboard shortcuts
                  </Button>
                }
              >
                <table className="w-full text-sm">
                  <caption className="sr-only">Keyboard shortcuts</caption>
                  <tbody>
                    {shortcuts(gate, deciding).map(([keys, what]) => (
                      <tr key={keys} className="align-top">
                        <th scope="row" className="whitespace-nowrap py-1 pr-3 text-left font-mono text-xs font-normal text-fg">
                          {keys}
                        </th>
                        <td className="py-1 text-fg-muted">{what}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Popover>
            </div>
          </header>

          <div className="aspect-square min-h-0 overflow-hidden rounded-token border sm:aspect-video lg:aspect-auto lg:flex-1">
            {stageSrc && item.kind === "image" ? (
              <ZoomedImage src={stageSrc} ratio={stageRatio} alt={`${assetLabel}, ${shown?.ratio} rendition`} zoom={zoom} onZoom={setZoom} />
            ) : stageSrc ? (
              <video
                key={stageSrc}
                src={stageSrc}
                poster={shown ? mediaContentUrl(shown.media_id, "poster") : undefined}
                controls
                muted
                playsInline
                preload="metadata"
                aria-label={`${assetLabel}, ${shown?.ratio} video`}
                className="size-full bg-surface object-contain"
              />
            ) : null}
          </div>

          {item.renditions.length > 0 ? (
            <div role="group" aria-label="Renditions" className="flex flex-wrap gap-1">
              {item.renditions.map((option, index) => {
                const on = option === shown;
                return (
                  <Button
                    key={option.media_id}
                    size="sm"
                    variant={on ? "secondary" : "ghost"}
                    aria-pressed={on}
                    onClick={() => {
                      setRendition(index);
                      setZoom(FIT);
                    }}
                    className="tabular-nums"
                  >
                    <span className="font-mono text-xs">{option.surface}</span>
                    {option.ratio} · {option.px.replace("x", "×")}
                    {option.duration_ms !== null ? ` · ${(option.duration_ms / 1000).toFixed(1)} s` : ""}
                    {option.lint_verdict ? <span className="text-xs text-fg-muted">lint {option.lint_verdict.replaceAll("_", " ")}</span> : null}
                  </Button>
                );
              })}
            </div>
          ) : null}
        </section>

        {/* The inspector. */}
        <aside aria-label="Review" className="flex min-h-0 flex-col gap-5 lg:w-84 lg:shrink-0 lg:overflow-y-auto lg:pr-1">
          <ReferenceCompare
            key={item.asset_id}
            references={compared.refs}
            asset={stageSrc && item.kind === "image" ? { src: stageSrc, ratio: stageRatio, label: assetLabel } : null}
            zoom={zoom}
            onZoom={setZoom}
            emptyReason={compared.reason}
          />

          {deciding ? (
            <>
              <ReviewChecklist checklist={current.checklist} onToggle={toggle} />
              <DecisionControls
                gate={gate}
                noun={noun}
                current={current}
                onApprove={() => apply("approve")}
                onReject={() => apply("reject")}
                onRegenerate={() => setNoteOpen(true)}
                onClear={clear}
              />
            </>
          ) : approval.status === "pending" ? (
            <p className="rounded-token border px-3 py-2.5 text-sm text-fg">
              Awaiting {awaiting}
              <span className="mt-0.5 block text-xs text-fg-muted">
                Only whoever this gate is routed to decides it. You can look through every asset.
              </span>
            </p>
          ) : (
            <section aria-labelledby="recorded-title" className="flex flex-col gap-2">
              <h2 id="recorded-title" className="text-sm font-medium text-fg">
                Recorded
              </h2>
              <p className="flex items-center gap-2 text-sm text-fg">
                {current.decision ? DECISION_LABEL[current.decision] : "No decision recorded"}
                {current.note ? <span className="text-fg-muted">— {current.note}</span> : null}
              </p>
              <ReviewChecklist checklist={current.checklist} />
            </section>
          )}

          <section aria-labelledby="advisory-title" className="flex flex-col gap-1.5">
            <h2 id="advisory-title" className="text-sm font-medium text-fg">
              Advisory
            </h2>
            {item.vision_advisory.notes.length === 0 && item.vision_advisory.flags.length === 0 ? (
              <p className="text-sm text-fg-muted">VISION raised nothing on this {noun}.</p>
            ) : (
              <>
                {item.vision_advisory.flags.length > 0 ? (
                  <ul aria-label="Flags" className="flex flex-wrap gap-1">
                    {item.vision_advisory.flags.map((flag) => (
                      <li key={flag}>
                        <Badge>{flag.replaceAll("_", " ")}</Badge>
                      </li>
                    ))}
                  </ul>
                ) : null}
                <ul className="flex list-disc flex-col gap-1 pl-4 text-sm text-fg-muted">
                  {item.vision_advisory.notes.map((note) => (
                    <li key={note}>{note}</li>
                  ))}
                </ul>
              </>
            )}
            <p className="text-xs text-fg-subtle">Advice from the vision model. It decides nothing.</p>
          </section>
        </aside>
      </div>

      {refusal ? (
        <Alert tone="error" title="Not recorded">
          {refusal}
        </Alert>
      ) : null}

      {/* The tally and the one way to record it. */}
      <footer className="sticky bottom-0 flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-t bg-bg py-2.5">
        <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1 text-sm">
          <span className="tabular-nums text-fg" data-testid="review-tally">
            {tallyLine(counts, gate, regenerateUsd)}
          </span>
          {deciding ? (
            <span className="tabular-nums text-fg-muted">
              {counts.undecided === 0 ? "Every asset decided" : `${counts.undecided} undecided`}
            </span>
          ) : null}
          {deciding ? <SaveState save={save} /> : null}
        </div>
        {deciding ? (
          <Button
            variant={allDecided ? "primary" : "secondary"}
            onClick={openSubmit}
            disabled={!allDecided}
            aria-keyshortcuts="Meta+Enter Control+Enter"
          >
            Review {items.length} decisions
          </Button>
        ) : approval.status === "pending" ? (
          <span className="text-sm text-fg-muted">Awaiting {awaiting}</span>
        ) : (
          <span className="text-sm text-fg-muted">
            Recorded {approval.decided_at ? absoluteTime(approval.decided_at) : ""}
          </span>
        )}
      </footer>

      {deciding && gate === "G8" ? (
        <RegenerateNote
          open={noteOpen}
          onOpenChange={setNoteOpen}
          noun={noun}
          assetId={item.asset_id}
          initial={current.note ?? ""}
          onSave={(note) => {
            setNoteOpen(false);
            apply("regenerate", note);
          }}
        />
      ) : null}

      {deciding ? (
        <Dialog open={submitOpen} onOpenChange={recording ? undefined : setSubmitOpen}>
          <DialogContent
            title={`Record the ${gate === "G8" ? "media review" : "media re-review"}`}
            description={`${items.length} assets, decided one by one. Recording is final for this gate.`}
            onOpenAutoFocus={(event) => {
              event.preventDefault();
              document.getElementById("review-record")?.focus();
            }}
          >
            <DialogBody>
              <p className="text-base font-medium tabular-nums text-fg" data-testid="review-submit-tally">
                {tallyLine(counts, gate, regenerateUsd)}
              </p>
              <ul className="flex flex-col gap-1.5 text-sm text-fg-muted">
                <li>
                  <Check aria-hidden className="mr-1.5 inline size-4 text-status-success" />
                  {counts.approve} approved {counts.approve === 1 ? "asset goes" : "assets go"} on to the package.
                </li>
                <li>
                  <X aria-hidden className="mr-1.5 inline size-4 text-status-failed" />
                  {counts.reject} rejected {counts.reject === 1 ? "asset is" : "assets are"} dropped.
                </li>
                {gate === "G8" ? (
                  <li>
                    <RotateCcw aria-hidden className="mr-1.5 inline size-4 text-status-gate-ink" />
                    {counts.regenerate === 0
                      ? "Nothing is regenerated, so there is no re-review."
                      : regenerateUsd === null
                        ? `Regenerating ${counts.regenerate} ${counts.regenerate === 1 ? "asset" : "assets"} is being priced.`
                        : `Regenerating ${counts.regenerate} ${counts.regenerate === 1 ? "asset" : "assets"} spends ≈ ${usd(regenerateUsd)}` +
                          (media && remainingAfter !== null
                            ? ` · ${usd(Math.max(0, remainingAfter))} of ${usd(Number(media.media.cap_usd))} media budget remains.`
                            : ".") +
                          " They come back once, at G8b."}
                  </li>
                ) : (
                  <li>There is no second regeneration: G8b is approve or reject.</li>
                )}
              </ul>
              {remainingAfter !== null && remainingAfter < 0 ? (
                <Alert tone="warning" title="Over the media budget">
                  These regenerations need ≈ {usd(regenerateUsd ?? 0)} and {usd(Number(media?.remaining_usd ?? 0))} remains.
                  Regenerate fewer assets, or reject the ones that cannot be regenerated.
                </Alert>
              ) : null}
              {priceError ? (
                <Alert tone="warning" title="Regeneration price unavailable">
                  {priceError instanceof ApiError ? priceError.detail : "The regeneration price could not be read."} The
                  server checks the budget again when it runs them.
                </Alert>
              ) : null}
            </DialogBody>
            <DialogFooter>
              <Button variant="secondary" onClick={() => setSubmitOpen(false)} disabled={recording}>
                Cancel
              </Button>
              <Button id="review-record" onClick={() => void record()} disabled={recording}>
                {recording ? <Spinner label="Recording" /> : null}
                Record {items.length} decisions
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      ) : null}
    </div>
  );
}

function SaveState({ save }: { save: Save }) {
  const text =
    save.kind === "saving" || save.kind === "pending"
      ? "Saving draft…"
      : save.kind === "saved"
        ? `Draft saved ${absoluteTime(save.at)}`
        : save.kind === "failed"
          ? `Draft not saved: ${save.detail}. Your decisions are kept here; the next change retries.`
          : "Every change saves as a draft";
  return (
    <span
      role="status"
      className={cn("text-xs", save.kind === "failed" ? "text-status-failed" : "text-fg-muted")}
      data-testid="review-save"
      data-save={save.kind}
    >
      {text}
    </span>
  );
}

function DecisionControls({
  gate,
  noun,
  current,
  onApprove,
  onReject,
  onRegenerate,
  onClear,
}: {
  gate: ReviewGate;
  noun: string;
  current: ReviewState[string];
  onApprove: () => void;
  onReject: () => void;
  onRegenerate: () => void;
  onClear: () => void;
}) {
  const ready = canApprove(current);
  const count = ticked(current.checklist);
  return (
    <section aria-labelledby="decision-title" className="flex flex-col gap-2">
      <h2 id="decision-title" className="text-sm font-medium text-fg">
        Decision
      </h2>
      <div className="flex flex-wrap gap-2">
        <Button
          onClick={onApprove}
          disabled={!ready}
          aria-keyshortcuts="A"
          aria-describedby={ready ? undefined : "approve-why"}
          aria-pressed={current.decision === "approve"}
        >
          <Check aria-hidden />
          Approve {noun}
          <kbd className="font-mono text-xs opacity-80">A</kbd>
        </Button>
        <Button variant="secondary" onClick={onReject} aria-keyshortcuts="R" aria-pressed={current.decision === "reject"}>
          <X aria-hidden />
          Reject {noun}
          <kbd className="font-mono text-xs text-fg-muted">R</kbd>
        </Button>
        {gate === "G8" ? (
          <Button variant="secondary" onClick={onRegenerate} aria-keyshortcuts="G" aria-pressed={current.decision === "regenerate"}>
            <RotateCcw aria-hidden />
            Regenerate {noun}
            <kbd className="font-mono text-xs text-fg-muted">G</kbd>
          </Button>
        ) : null}
      </div>
      {ready ? null : (
        <p id="approve-why" className="text-xs tabular-nums text-fg-muted">
          Tick all four checks to approve · {count} of 4 ticked
        </p>
      )}
      {current.decision ? (
        <p className="flex flex-wrap items-center gap-x-2 text-sm text-fg" data-testid="review-decision">
          <span>
            Decided: <strong className="font-medium">{DECISION_LABEL[current.decision]}</strong>
            {current.decision === "regenerate" && current.note ? <span className="text-fg-muted"> — {current.note}</span> : null}
          </span>
          <Button variant="ghost" size="sm" onClick={onClear} aria-keyshortcuts="Backspace">
            Clear decision
          </Button>
        </p>
      ) : null}
    </section>
  );
}

/** `G`: what the regeneration should change, and what it costs, before it is chosen. */
function RegenerateNote({
  open,
  onOpenChange,
  noun,
  assetId,
  initial,
  onSave,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  noun: string;
  assetId: string;
  initial: string;
  onSave: (note: string) => void;
}) {
  const [note, setNote] = useState(initial);
  const [estimate] = useRegenerationEstimates(open ? [assetId] : []);
  useEffect(() => {
    if (open) setNote(initial);
  }, [open, initial]);
  const ready = note.trim().length > 0;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        title={`Regenerate this ${noun}`}
        description="Say what should change. It runs once, after this review is recorded, with the run's model, and comes back for re-review at G8b."
      >
        <DialogBody>
          <label htmlFor="regenerate-note" className="text-sm font-medium text-fg">
            What should change
          </label>
          <Textarea
            id="regenerate-note"
            autoFocus
            value={note}
            maxLength={2000}
            onChange={(event) => setNote(event.target.value)}
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === "Enter" && ready) {
                event.preventDefault();
                onSave(note);
              }
            }}
            placeholder="The label on the bottle is unreadable; keep the composition."
          />
          <p className="text-xs tabular-nums text-fg-muted" aria-live="polite">
            {estimate?.data
              ? `≈ ${usd(Number(estimate.data.estimate_usd))} at ${estimate.data.model_id} · ${usd(Number(estimate.data.remaining_usd))} of ${usd(Number(estimate.data.media.cap_usd))} media budget remains now`
              : estimate?.isError
                ? `Price unavailable: ${estimate.error instanceof ApiError ? estimate.error.detail : "the estimate failed"}`
                : "Pricing this regeneration…"}
          </p>
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={() => onSave(note)} disabled={!ready} aria-keyshortcuts="Meta+Enter Control+Enter">
            Regenerate {noun}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
