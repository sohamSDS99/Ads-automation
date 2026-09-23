# UI-REVIEW — S3-P7, Frontend A

Retroactive visual audit of the six Stage 03 surfaces, graded against the
contract in [ui-spec-s3p7.md](ui-spec-s3p7.md). Scale: **1** broken · **2**
serviceable · **3** good · **4** exemplary.

**Method.** The compose stack could not be started — Docker's containerd is
wedged on a blob with an `input/output error`, the same failure S3-P5 and
S3-P6 hit — so the web tier was built for production, served with `next start`,
and pointed at a stdlib stand-in for `api` serving the shipped response shapes.
155 automated checks across five routes × two viewports (1440, 390) × two roles
(`operator`, `viewer`), plus screenshots read by eye. What that proves and what
it does not is stated under *Limits* at the foot.

---

## Grades

| # | Pillar | Grade | Why |
|---|---|---|---|
| 1 | Layout & composition | **3** | Four stacked blocks answer the four questions in the order they are asked. The rulebook is a sticky index plus a column of prose, not a card grid. No nested cards anywhere. Loses a point for the DAG canvas inheriting Stage 01's small default fit on an 18-node graph. |
| 2 | Typography & rhythm | **3** | One family, fixed rem scale, 8px grid — the incumbent system, unchanged. The executive summary is held to a ~70ch measure; tables run denser, which is correct for data. Monospace is confined to hashes, versions, node ids and constant keys. |
| 3 | Colour & contrast | **4** | Only three things in the whole stage are coloured: severity, the two rail dots, and the `unverified` flag. Everything else is the neutral scale. Colour is never the only carrier — every dot has an `sr-only` sentence, every severity chip carries its word. |
| 4 | Hierarchy & information design | **4** | The strongest pillar and the point of the stage. Every rule renders its severity *and* its authority with a review date; every spec cell carries its `content_constants.yaml` key; the `not_applicable` policy list is collapsed but present. A reader can always get from a rule to who said so. |
| 5 | States & feedback | **3** | Skeletons not spinners, empty states that teach, named absences per rulebook section, a publish dialog that lists every blocker rather than the first. Loses a point because the publish success path could not be exercised against a real API. |
| 6 | Accessibility & responsive | **3** | Links with `aria-current` rather than fake tabs; disclosure buttons rather than a lying `role="tree"`; badge sentences in the accessible name. 390 and 1440 both clean after the fix below. Not yet checked with a real screen reader or at 200% zoom. |

---

## What the screenshots found that 155 passing checks did not

Four defects survived a green suite and died to a human looking at a picture.
They are listed because the pattern matters more than the fixes: every one of
them is a thing no assertion was ever going to be written for.

1. **The rail label truncated to `03…`.** The status chip and the label were
   competing for a 240px rail, and the chip won — so the row describing
   *Content guidelines* stopped saying so. The shipped `stageChip` docstring
   had predicted exactly this for a shorter string. Fixed by moving the chip to
   a second line beneath the label.
2. **The asset spec matrix was transposed the wrong way.** Asset types as
   columns put the fifth one off-screen at 1440 inside the table's own
   scroller, so a complete matrix read as one with data missing. Asset types
   are now the rows and campaign types the columns.
3. **The `unverified` flag stretched to its column** — a flex child with no
   `self-start` — so it read as a filled cell rather than a chip, which is the
   opposite of the quiet-but-unmissable it needs to be.
4. **`5120 KB`** where a person would have said **`5 MB`**.

And one the automation did catch, worth recording because the fix is
structural: the eight-column version history forced the **page** to scroll
sideways by 403px at 390 — measured with `window.scrollTo`, not inferred from
`scrollWidth`, because a clipped box still inflates the latter. `Table` already
ships its own `overflow-x-auto` and it was not enough. Five columns now drop
below `sm` and the created date rides under the version label instead.

---

## Against the contract

| Contract clause | Status |
|---|---|
| §15.1 r1 — the 03 row is never locked | ✅ asserted in both roles, both widths; there is no code path that could render one |
| §15.1 r2 — status chip | ✅ five states, `Not started` deliberately renders as no chip |
| §15.1 r3 — two badges | ✅ red = the caller's open task, amber = the project's; each carries its sentence |
| §15.3 A — four blocks | ✅ including the Attention block and the eight-column history |
| §15.3 B — Rules tab | ✅ matcher in plain language, severity, authority, scope |
| §15.3 E — five sections + severity/authority chips | ✅ all seven anchors, `not_applicable` collapsed but present |
| §15.3 H — publish dialog | ✅ gates, signature, critique and counts above the confirm field |
| §15.4 r2 — no rule evaluation in TypeScript | ✅ `describeMatcher` describes; nothing evaluates |
| §15.4 r3 — controls absent, not disabled | ✅ Start, Publish and the rule-test control are all absent without the permission |
| §15.4 r6 — zod generated from JSON Schema | ❌ **not done** — types are hand-written from the Python contracts. See the PR. |

---

## Limits

This audit proves the **render**: that every route loads, every state appears,
controls are present or absent by role, nothing overflows at either width, and
no console error fires. It does **not** prove the phase's exit criteria end to
end. A real run streaming 18 nodes over SSE, a real publish minting a real
ruleset, and the `409`-with-every-blocker path were exercised against a
stand-in, not against `api`. They need the compose stack, and the compose stack
needs a Docker Desktop restart — which kills every other session's containers
and is therefore not this session's call to make.

Re-run when Docker is healthy: `make browser-s3p7` does not exist yet; the
harness used here lives in this session's scratchpad and should be ported to
`apps/api/scripts/browser-check-s3p7.py` against the real stack, following the
pattern of the other twelve.
