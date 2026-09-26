/**
 * The G8 / G8b review, as state (Stage 04 PRD §15.4 H).
 *
 * Pure functions over one reviewer's decisions, keyed by asset id, so the
 * workspace's keyboard handler, its autosave and its submit read one model and
 * the rules can be tested without a browser. The rules here are the
 * *interface's* — Approve stays disabled until four explicit ticks, a
 * regeneration needs a note, G8b offers no regeneration — and every one of
 * them is enforced again by the server (`creative/review.check_submission`),
 * which is the only authority.
 */
import type {
  ItemDecision,
  ReviewChecklist,
  ReviewDecision,
  ReviewDecisionItem,
  ReviewDraft,
  ReviewGate,
  ReviewItem,
} from "@/lib/api/review";

export type CheckKey = keyof ReviewChecklist;

/** The four ticks, in the order keys 1–4 toggle them. */
export const CHECKS: readonly { key: CheckKey; label: string; hint: string }[] = [
  { key: "label_ok", label: "Label correct", hint: "The AI-generated disclosure is present and reads right." },
  { key: "product_match_ok", label: "Product matches", hint: "The product shown is the real thing, as the reference shows it." },
  { key: "subjects_ok", label: "Subjects allowed", hint: "Nobody and nothing appears that the brand rules exclude." },
  { key: "rights_ok", label: "Rights clear", hint: "Nothing in the frame needs a licence we do not hold." },
];

export type ItemState = {
  decision: ReviewDecision | null;
  checklist: ReviewChecklist;
  note: string | null;
};

export type ReviewState = Readonly<Record<string, ItemState>>;

export type Tally = { approve: number; reject: number; regenerate: number; undecided: number };

export function emptyChecklist(): ReviewChecklist {
  return { label_ok: false, product_match_ok: false, subjects_ok: false, rights_ok: false };
}

function blank(): ItemState {
  return { decision: null, checklist: emptyChecklist(), note: null };
}

/** Only JSON `true` is a tick — what the server's `StrictBool` accepts. */
function checklistOf(value: unknown): ReviewChecklist {
  const source = (typeof value === "object" && value !== null ? value : {}) as Record<string, unknown>;
  return {
    label_ok: source["label_ok"] === true,
    product_match_ok: source["product_match_ok"] === true,
    subjects_ok: source["subjects_ok"] === true,
    rights_ok: source["rights_ok"] === true,
  };
}

const DECISIONS: readonly ReviewDecision[] = ["approve", "reject", "regenerate"];

/**
 * Where a reviewer left off: the saved draft laid over the card. A draft
 * entry for an asset no longer on the card is ignored; a decision the gate
 * does not offer (regenerate at G8b), or an approve whose ticks are not all
 * there, comes back undecided rather than as a decision Approve could not
 * have made.
 */
export function initialState(
  items: readonly ReviewItem[],
  draft: Partial<ReviewDraft> | null | undefined,
  gate: ReviewGate,
): ReviewState {
  const state: Record<string, ItemState> = {};
  for (const item of items) state[item.asset_id] = blank();
  for (const saved of Array.isArray(draft?.items) ? draft.items : []) {
    if (!saved || !(saved.asset_id in state)) continue;
    const checklist = checklistOf(saved.checklist);
    const note = typeof saved.note === "string" && saved.note.trim() ? saved.note : null;
    let decision = DECISIONS.includes(saved.decision as ReviewDecision)
      ? (saved.decision as ReviewDecision)
      : null;
    if (decision === "approve" && !allTicked(checklist)) decision = null;
    if (decision === "regenerate" && (gate !== "G8" || note === null)) decision = null;
    state[saved.asset_id] = { decision, checklist, note };
  }
  return state;
}

/** A decided card, as it was recorded — read-only. */
export function recordedState(items: readonly ReviewItem[]): ReviewState {
  const state: Record<string, ItemState> = {};
  for (const item of items) {
    const recorded: ItemDecision | null = item.decision;
    state[item.asset_id] = recorded
      ? { decision: recorded.decision, checklist: checklistOf(recorded.checklist), note: recorded.note }
      : blank();
  }
  return state;
}

/** The asset the draft had open, if it is still on the card. */
export function initialCursor(items: readonly ReviewItem[], draft: Partial<ReviewDraft> | null | undefined): number {
  const at = items.findIndex((item) => item.asset_id === draft?.cursor);
  return at >= 0 ? at : 0;
}

export function ticked(checklist: ReviewChecklist): number {
  return CHECKS.filter(({ key }) => checklist[key]).length;
}

export function allTicked(checklist: ReviewChecklist): boolean {
  return ticked(checklist) === CHECKS.length;
}

export function canApprove(item: ItemState): boolean {
  return allTicked(item.checklist);
}

export function offers(gate: ReviewGate, decision: ReviewDecision): boolean {
  return decision !== "regenerate" || gate === "G8";
}

/**
 * Toggle one tick. Unticking an approved asset withdraws the approval — an
 * approval is the four ticks, so it cannot outlive one of them — and says so.
 */
export function toggleCheck(item: ItemState, key: CheckKey): { next: ItemState; withdrew: boolean } {
  const checklist = { ...item.checklist, [key]: !item.checklist[key] };
  const withdrew = item.decision === "approve" && !allTicked(checklist);
  return { next: { ...item, checklist, decision: withdrew ? null : item.decision }, withdrew };
}

export type Refusal = "needs_ticks" | "needs_note" | "not_offered";

/** Decide one asset, or say why the interface will not. */
export function decide(
  item: ItemState,
  decision: ReviewDecision,
  gate: ReviewGate,
  note?: string,
): { next: ItemState } | { refused: Refusal } {
  if (!offers(gate, decision)) return { refused: "not_offered" };
  if (decision === "approve" && !canApprove(item)) return { refused: "needs_ticks" };
  if (decision === "regenerate") {
    const text = (note ?? "").trim();
    if (!text) return { refused: "needs_note" };
    return { next: { ...item, decision, note: text } };
  }
  return { next: { ...item, decision } };
}

export function clearDecision(item: ItemState): ItemState {
  return { ...item, decision: null };
}

export function tally(state: ReviewState, order: readonly string[]): Tally {
  const counts: Tally = { approve: 0, reject: 0, regenerate: 0, undecided: 0 };
  for (const id of order) {
    const decision = state[id]?.decision ?? null;
    if (decision === null) counts.undecided += 1;
    else counts[decision] += 1;
  }
  return counts;
}

/** The next undecided asset after `from`, wrapping; null when every one is decided. */
export function nextUndecided(state: ReviewState, order: readonly string[], from: number): number | null {
  for (let step = 1; step <= order.length; step += 1) {
    const at = (from + step) % order.length;
    const id = order[at];
    if (id !== undefined && (state[id]?.decision ?? null) === null) return at;
  }
  return null;
}

/** What the autosave sends: every asset somebody has touched, and where they are. */
export function toDraft(state: ReviewState, order: readonly string[], cursor: string | null): ReviewDraft {
  const items = order
    .map((id) => ({ asset_id: id, ...(state[id] ?? blank()) }))
    .filter((item) => item.decision !== null || ticked(item.checklist) > 0 || item.note !== null);
  return { items, cursor };
}

/** `edited_proposal.items[]`, or the assets still undecided. */
export function toSubmission(
  state: ReviewState,
  order: readonly string[],
): { items: ReviewDecisionItem[] } | { undecided: string[] } {
  const undecided = order.filter((id) => (state[id]?.decision ?? null) === null);
  if (undecided.length > 0) return { undecided };
  return {
    items: order.map((id) => {
      const item = state[id] as ItemState & { decision: ReviewDecision };
      const sent: ReviewDecisionItem = { asset_id: id, decision: item.decision, checklist: { ...item.checklist } };
      if (item.decision === "regenerate" && item.note) sent.note = item.note;
      return sent;
    }),
  };
}

/** Sum decimal-string prices in cents, so four estimates never add to $1.7999999. */
export function sumUsd(values: readonly string[]): number {
  return values.reduce((cents, value) => cents + Math.round(Number(value) * 100), 0) / 100;
}

export function usd(value: number): string {
  return `$${value.toFixed(2)}`;
}

/**
 * The submit line (§15.4 H): "Approve 14 · Reject 2 · Regenerate 3 (≈ $1.80)".
 * G8b has no Regenerate. `regenerateUsd` is null while it is being priced and
 * omitted where no price belongs (a recorded card).
 */
export function tallyLine(counts: Tally, gate: ReviewGate, regenerateUsd?: number | null): string {
  const parts = [`Approve ${counts.approve}`, `Reject ${counts.reject}`];
  if (gate === "G8") {
    const price =
      counts.regenerate === 0 || regenerateUsd === undefined
        ? ""
        : regenerateUsd === null
          ? " (pricing…)"
          : ` (≈ ${usd(regenerateUsd)})`;
    parts.push(`Regenerate ${counts.regenerate}${price}`);
  }
  return parts.join(" · ");
}

export const DECISION_LABEL: Record<ReviewDecision, string> = {
  approve: "Approved",
  reject: "Rejected",
  regenerate: "Regenerate",
};
