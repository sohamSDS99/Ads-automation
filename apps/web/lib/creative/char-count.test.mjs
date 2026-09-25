/**
 * The counter parity test (Stage 04 PRD §15.5 item 1): the Ad Studio's
 * `CharCounter` shows exactly the count the server's linter measures, for 200
 * strings including emoji, CJK and keyword insertions, on both surfaces.
 *
 * The fixture is the server's count — `apps/api/scripts/make_char_count_fixture.py`
 * writes it with the linter's own `measure()`, and the api suite fails when the
 * file drifts from it. Run with `pnpm test:unit`.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { charCount } from "./char-count.ts";

const fixture = JSON.parse(
  readFileSync(new URL("../../tests/fixtures/char-count-parity.json", import.meta.url), "utf8"),
);

test("the fixture is the 200 strings §15.5 names", () => {
  assert.equal(fixture.unit, "chars");
  assert.equal(fixture.cases.length, 200);
});

for (const surface of ["rsa_headline", "rsa_description"]) {
  test(`the client counts every string as the server does on ${surface}`, () => {
    const mismatches = fixture.cases
      .map(({ text, counts }) => ({ text, server: counts[surface], client: charCount(surface, text) }))
      .filter(({ server, client }) => server !== client);
    assert.deepEqual(mismatches, []);
  });
}
