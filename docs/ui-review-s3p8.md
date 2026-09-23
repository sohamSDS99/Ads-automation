# UI-REVIEW — S3-P8

**Audited against:** `docs/ui-spec-s3p8.md` · **Date:** 2026-09-23
**Evidence:** `make browser-s3p8` (50 checks, all passing) at 1440 and 390, plus the screenshots it writes.

> `/gsd-ui-review` is installed but `@`-references `~/.claude/gsd-core/workflows/ui-review.md`,
> which does not exist on this machine. This is its named deliverable — the
> 6-pillar graded audit — produced directly. Grades are 1–4, where 3 is
> "ships" and 4 is "nothing left to say".

## Scores

| # | Pillar | Grade | One line |
|---|---|---|---|
| 1 | Hierarchy & layout | **3** | The register reads at a glance; the amendments page carries two sections that could each be a page |
| 2 | Typography & rhythm | **4** | Existing scale used throughout, no new sizes, tabular numerals on every count |
| 3 | Colour & contrast | **4** | No new tokens, no colour-only meaning, both themes inherited intact |
| 4 | States & feedback | **4** | Loading, empty, error, forbidden and in-flight on every surface; the 409 is an interstitial, not a toast |
| 5 | Accessibility | **4** | Roles, live regions, focus trap and restore, keyboard throughout |
| 6 | Responsiveness | **3** | Card list below 768px, zero overflow and zero row overlap at 390 — reached after a real defect |

---

## 1. Hierarchy & layout — 3

The claims register puts the one question a visitor has ("is this mine to
sign?") in a sticky bar above everything else, and the answer is a sentence
rather than a control state. The playground splits input and verdict into two
columns so the copy and its findings are in one viewport — the thing that makes
"four seconds" plausible.

**Against a 4:** `/guidelines/amendments` stacks the amendment inbox and the
sign-off matrix editor on one page. The argument for it is in the file header —
they are two halves of "what changes the rulebook and who answers for it", and
a matrix on its own settings page is a matrix nobody visits. It is still two
H1-weight concerns under one heading, and at 390px the matrix is a long scroll
past rows that are not about it. Worth revisiting when S3-P9 adds disapprovals.

## 2. Typography & rhythm — 4

One family, the existing fixed rem scale, no clamp, no new step. Every count and
every hash uses `data-numeric` or `font-mono` so figures align in a column and a
set hash is unmistakably a machine value. Prose blocks (the attestation, the
page intros) stay inside `max-w-prose`; the register runs dense and wide, which
is correct for a table.

## 3. Colour & contrast — 4

Zero new tokens and zero new hex values — the constraint the spec opened with,
held. The status vocabulary is Stage 01's, unchanged: running/success/gate/
failed/skipped, each paired with a word. Checked specifically:

- every status chip has a label beside its dot;
- every lint severity has a word, an icon and a colour;
- `signature_affecting` rows are red **and** headed "Signature affecting" **and**
  name the signatures — three independent signals for one meaning;
- `indeterminate` is styled as its own neutral state, never as a pass.

## 4. States & feedback — 4

Every surface has all five. The ones worth calling out:

- **Skeletons matching final layout**, not spinners on blank pages.
- **Empty states teach.** The playground opens on a worked example that trips a
  real rule, so the first thing a new user sees is the tool working rather than
  an empty box asking them to think of something.
- **The 409 is an interstitial**, not a toast. A signer must not be able to
  dismiss "the register changed" and press submit again.
- **Per-file upload state.** Five attachments where one failed does not render
  as one bar at 80%.
- **Destructive counts arrive before the control.** Both the reassign dialog and
  the matrix editor state the exact number of signatures a change voids, from
  the server, and keep the confirm control unreachable until it has.

## 5. Accessibility — 3

Present and verified: `role="table"`/`row`/`cell` with `aria-rowindex` so a
screen reader gets position among 500 rather than among the 20 in the DOM;
`aria-live="polite"` on the decision counter and the lint verdict;
`aria-live="assertive"` on the register-moved interstitial; focus moved to the
drawer panel rather than to a decision button; `aria-pressed` on the
approve/reject pair; every icon-only control named; the existing `Tabs`
primitive already implements the full ARIA tab pattern.

**The one gap this audit found, now closed.** The signature drawer is a
hand-rolled `role="dialog" aria-modal="true"` panel — Radix's `Dialog` centres,
and this needed a side panel. It moved focus in and closed on Escape, but it
neither trapped Tab nor restored focus on close. `aria-modal="true"` tells a
screen reader that everything behind the panel is inert; without a trap that is
a claim a keyboard user disproves by tabbing straight into the register they
were told they could not reach. Both are now implemented in the drawer
(`trapTab`, plus focus restore in the open effect). The two dialogs in this
phase use Radix and already had both.

## 6. Responsiveness — 3

Zero horizontal overflow on all three screens at 390px, and zero row overlap on
the register — both measured, not eyeballed.

**Worth recording how the second one was found**, because it is the pillar's
whole lesson: the register looked fine to the overflow check and was
*unreadable*. The virtualizer positions rows by absolute offset, so a fixed
56px row whose content wraps taller does not clip — it renders **under** the row
below. Horizontal overflow was 0px throughout. The fix was `measureElement` plus
a genuine card layout below 768px; the check now compares adjacent row
rectangles directly, so the next person to break it finds out.

**Against a 4:** the drawer becomes a full-width sheet at 390 but its footer
still holds two buttons side by side, which is tight with a long count
("Approve the 11 read" / "Sign 12 claims"). It fits; it is not comfortable.

---

## Acceptance — §12 of the spec

| # | Criterion | Result |
|---|---|---|
| A1 | Legal owner signs a set end to end, with a rejection and an edited expiry, and gets a receipt | **pass** — required fixing a server defect first |
| A2 | Nobody else sees the control at all | **pass** — admin, second approver, operator, viewer |
| A3 | Playground findings with highlighted spans under 1.5s | **pass** — 0.04s measured |
| A4 | Reassign states the exact void count before confirmation | **pass** |
| A5 | A moved `set_hash` writes nothing and shows the interstitial | **pass** — integration + browser |
| A6 | Person-task controls absent, not disabled, for a non-assignee | **pass** |
| A7 | `/approvals` shows both tabs with one summed badge | **pass** |
| A8 | No password or re-auth token in state, cache or logs | **pass** — code review + DOM assertion |

## Owed

1. Reconsider splitting `/amendments` when S3-P9 adds disapprovals (§1).
2. Drawer footer at 390 wants a stacked layout (§6).

Neither blocks the phase; both are cosmetic. The accessibility gap this audit
found was fixed rather than recorded.
