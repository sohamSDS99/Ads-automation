/**
 * The words an ad headline and a landing page's H1 share, and the words only
 * one of them has — for `WordDiff` on the Landing audit (Stage 04 PRD §15.4 J).
 *
 * **A reading aid, never the score.** Whether the page matches is 4.5.1's
 * `match.token_trigram_v1` against the threshold, and the card prints that
 * number beside this. The diff only shows a reader *which* words differ: a
 * longest common subsequence over words, compared case-folded with punctuation
 * dropped, so "SDS software," and "sds Software" are the same word and the
 * order of the shared words is kept. A token with no letter or digit (a `|`,
 * a dash) takes no side.
 */

export type DiffKind = "shared" | "only" | "neutral";
export type DiffToken = { text: string; kind: DiffKind };
export type WordDiff = { a: DiffToken[]; b: DiffToken[] };

function key(token: string): string {
  return token.normalize("NFKC").toLocaleLowerCase().replace(/[^\p{L}\p{N}]/gu, "");
}

function words(text: string): string[] {
  return text.split(/\s+/u).filter(Boolean);
}

export function wordDiff(a: string, b: string): WordDiff {
  const left = words(a);
  const right = words(b);
  const lk = left.map(key);
  const rk = right.map(key);
  // lcs[i][j]: the longest common run of left[i..] and right[j..], by key.
  const lcs = Array.from({ length: left.length + 1 }, () => new Array<number>(right.length + 1).fill(0));
  for (let i = left.length - 1; i >= 0; i -= 1) {
    for (let j = right.length - 1; j >= 0; j -= 1) {
      lcs[i]![j] =
        lk[i] !== "" && lk[i] === rk[j]
          ? lcs[i + 1]![j + 1]! + 1
          : Math.max(lcs[i + 1]![j]!, lcs[i]![j + 1]!);
    }
  }
  const sharedLeft = new Set<number>();
  const sharedRight = new Set<number>();
  let i = 0;
  let j = 0;
  while (i < left.length && j < right.length) {
    if (lk[i] !== "" && lk[i] === rk[j]) {
      sharedLeft.add(i);
      sharedRight.add(j);
      i += 1;
      j += 1;
    } else if (lcs[i + 1]![j]! >= lcs[i]![j + 1]!) {
      i += 1;
    } else {
      j += 1;
    }
  }
  const mark = (tokens: string[], keys: string[], shared: Set<number>): DiffToken[] =>
    tokens.map((text, index) => ({
      text,
      kind: keys[index] === "" ? "neutral" : shared.has(index) ? "shared" : "only",
    }));
  return { a: mark(left, lk, sharedLeft), b: mark(right, rk, sharedRight) };
}
