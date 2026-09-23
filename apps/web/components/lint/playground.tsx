"use client";

import { CircleSlash, ExternalLink, Loader2, ShieldAlert, ShieldCheck, TriangleAlert } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Alert } from "@/components/ui/alert";
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { ApiError } from "@/lib/api";
import {
  CAMPAIGN_TYPES,
  SURFACES,
  highlightRuns,
  lintText,
  type LintFinding,
  type LintResult,
  type Severity,
} from "@/lib/api/lint";
import { cn, httpUrl } from "@/lib/utils";

/** Long enough that a phrase settles, short enough that it feels live. */
const DEBOUNCE_MS = 400;

/**
 * The linter playground (PRD §15.3 F).
 *
 * "It turns the rulebook from a document people are supposed to have read into
 * something they can ask a question of in four seconds." Everything here serves
 * that sentence: no submit button, a 400 ms debounce, the previous request
 * aborted on the next keystroke, and the verdict in the same viewport as the
 * copy.
 *
 * **No rule is evaluated in this file.** Spans, severities and messages all come
 * from the server. The highlighting slices the original string on the offsets
 * the server returned — it never matches a rule back onto the text, which would
 * be rule evaluation in TypeScript wearing a hat (PRD §15.4 rule 2).
 */
export function LinterPlayground({ guidelineId }: { guidelineId: string }) {
  const [text, setText] = useState(SAMPLE);
  const [surface, setSurface] = useState<string>("rsa_headline");
  const [campaign, setCampaign] = useState<string>("search");
  const [market, setMarket] = useState("DE");
  const [language, setLanguage] = useState("en");
  const [result, setResult] = useState<LintResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef<AbortController | null>(null);

  const run = useCallback(
    async (copy: string) => {
      inFlight.current?.abort();
      if (!copy.trim()) {
        setResult(null);
        setError(null);
        return;
      }
      const controller = new AbortController();
      inFlight.current = controller;
      setBusy(true);
      try {
        const response = await lintText(
          guidelineId,
          [
            {
              ref: "playground",
              surface,
              campaign_type: campaign,
              market,
              language,
              text: copy,
            },
          ],
          controller.signal,
        );
        if (controller.signal.aborted) return;
        setResult(response.result);
        setError(null);
      } catch (caught) {
        if (controller.signal.aborted) return;
        setResult(null);
        setError(caught instanceof ApiError ? caught.detail : "The linter did not answer.");
      } finally {
        if (!controller.signal.aborted) setBusy(false);
      }
    },
    [guidelineId, surface, campaign, market, language],
  );

  useEffect(() => {
    const timer = setTimeout(() => void run(text), DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [text, run]);

  // An in-flight request outliving the screen would resolve into a dead tree.
  useEffect(() => () => inFlight.current?.abort(), []);

  // Same reason as the register: a fresh `[]` each render would re-slice the
  // whole string on every keystroke, on the screen whose budget is 1.5 s.
  const findings = useMemo(() => result?.findings ?? [], [result]);
  const runs = useMemo(() => highlightRuns(text, findings), [text, findings]);

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <section className="space-y-3">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Labelled label="Surface">
            <Select value={surface} onChange={(event) => setSurface(event.target.value)}>
              {Object.entries(
                SURFACES.reduce<Record<string, typeof SURFACES>>((groups, item) => {
                  (groups[item.group] ??= []).push(item);
                  return groups;
                }, {}),
              ).map(([group, items]) => (
                <optgroup key={group} label={group}>
                  {items.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.label}
                    </option>
                  ))}
                </optgroup>
              ))}
            </Select>
          </Labelled>
          <Labelled label="Campaign">
            <Select value={campaign} onChange={(event) => setCampaign(event.target.value)}>
              {CAMPAIGN_TYPES.map((item) => (
                <option key={item} value={item}>
                  {item.replace(/_/g, " ")}
                </option>
              ))}
            </Select>
          </Labelled>
          <Labelled label="Market">
            <Select value={market} onChange={(event) => setMarket(event.target.value)}>
              {["DE", "GB", "US", "FR", "NL"].map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </Select>
          </Labelled>
          <Labelled label="Language">
            <Select value={language} onChange={(event) => setLanguage(event.target.value)}>
              {["en", "de", "fr", "nl"].map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </Select>
          </Labelled>
        </div>

        <div>
          <label htmlFor="lint-copy" className="text-xs font-medium text-fg-subtle">
            Your copy
          </label>
          <Textarea
            id="lint-copy"
            rows={6}
            value={text}
            onChange={(event) => setText(event.target.value)}
            className="mt-1.5 font-mono text-sm"
            placeholder="Type or paste a headline."
          />
          <p data-numeric className="mt-1 text-xs text-fg-subtle">
            {text.length} characters
            {result ? ` · checked against ${result.rules_evaluated} rules in ${result.elapsed_ms} ms` : null}
          </p>
        </div>
      </section>

      <section className="space-y-3">
        <div aria-live="polite" className="flex items-center gap-2">
          <Verdict result={result} busy={busy} error={error} />
          {result ? (
            <span className="font-mono text-xs text-fg-subtle">{result.ruleset_version}</span>
          ) : null}
        </div>

        {error ? (
          <Alert tone="error" title="Could not check this">
            {error}
          </Alert>
        ) : null}

        {text.trim() && !error ? (
          <div className="rounded-[var(--radius)] border bg-surface-raised p-4">
            <p className="font-mono text-sm leading-relaxed whitespace-pre-wrap">
              {runs.map((run, index) =>
                run.severity ? (
                  <mark key={index} className={cn("rounded-sm px-0.5", MARK[run.severity])}>
                    {run.text}
                  </mark>
                ) : (
                  <span key={index}>{run.text}</span>
                ),
              )}
            </p>
          </div>
        ) : null}

        {findings.length > 0 ? (
          <ul className="space-y-2">
            {findings.map((finding, index) => (
              <FindingRow key={`${finding.rule_id}-${index}`} finding={finding} />
            ))}
          </ul>
        ) : result && !busy ? (
          <p className="text-sm text-fg-muted">
            Nothing in the rulebook objects to this copy.
          </p>
        ) : null}
      </section>
    </div>
  );
}

function Labelled({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs font-medium text-fg-subtle">{label}</span>
      {children}
    </label>
  );
}

const MARK: Record<Severity, string> = {
  blocking: "bg-status-failed/25 text-fg",
  warning: "bg-status-gate/25 text-fg",
  advisory: "bg-status-skipped/25 text-fg",
};

const SEVERITY_LABEL: Record<Severity, string> = {
  blocking: "Blocking",
  warning: "Warning",
  advisory: "Advisory",
};

/**
 * The verdict chip.
 *
 * `indeterminate` is never styled as a pass. It is what the server returns when
 * a check could not run at all, and law 31 makes a blocking indeterminate fail —
 * so a green tick here would be the exact failure the rule exists to prevent.
 */
function Verdict({
  result,
  busy,
  error,
}: {
  result: LintResult | null;
  busy: boolean;
  error: string | null;
}) {
  if (busy) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs text-fg-muted">
        <Loader2 aria-hidden className="size-3.5 animate-spin" />
        Checking
      </span>
    );
  }
  if (error || !result) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs text-fg-muted">
        <CircleSlash aria-hidden className="size-3.5" />
        Not checked
      </span>
    );
  }
  const indeterminate = result.findings.some((item) => item.indeterminate);
  if (indeterminate) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-border-strong px-2.5 py-1 text-xs font-medium text-fg">
        <CircleSlash aria-hidden className="size-3.5 text-fg-subtle" />
        Could not be checked
      </span>
    );
  }
  const map = {
    pass: { icon: ShieldCheck, label: "Passes", border: "border-status-success" },
    pass_with_warnings: { icon: TriangleAlert, label: "Passes with warnings", border: "border-status-gate" },
    fail: { icon: ShieldAlert, label: "Blocked", border: "border-status-failed" },
  } as const;
  const state = map[result.verdict];
  const Icon = state.icon;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium text-fg",
        state.border,
      )}
    >
      <Icon aria-hidden className="size-3.5" />
      {state.label}
    </span>
  );
}

function FindingRow({ finding }: { finding: LintFinding }) {
  return (
    <li className="rounded-[var(--radius)] border bg-surface-raised px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium text-fg",
            finding.severity === "blocking" && "border-status-failed",
            finding.severity === "warning" && "border-status-gate",
            finding.severity === "advisory" && "border-border-strong",
          )}
        >
          <span
            aria-hidden
            className={cn(
              "size-1.5 rounded-full",
              finding.severity === "blocking" && "bg-status-failed",
              finding.severity === "warning" && "bg-status-gate",
              finding.severity === "advisory" && "bg-status-skipped",
            )}
          />
          {SEVERITY_LABEL[finding.severity]}
        </span>
        <code className="font-mono text-xs text-fg-subtle">{finding.rule_id}</code>
        {finding.indeterminate ? (
          <span className="text-xs text-fg-muted">could not be checked</span>
        ) : null}
      </div>
      <p className="mt-1.5 text-sm text-fg">{finding.message}</p>
      {finding.fix_hint ? (
        <p className="mt-1 text-sm text-fg-muted">{finding.fix_hint}</p>
      ) : null}
      <Authority reference={finding.authority_ref} />
    </li>
  );
}

/**
 * Who said so. A rule with no traceable authority is an opinion.
 *
 * `authority_ref` is whatever the compiler put on the rule — a policy URL, a
 * signature id, a constants key. Two things follow, and both are handled by
 * `httpUrl` rather than by a regex at the call site: a value that merely
 * *starts* `https://` can still fail `new URL()` and take the whole findings
 * list down with it, and only `http`/`https` may reach an `href` — React does
 * not sanitize hrefs, so a `javascript:` value there would run on click.
 */
function Authority({ reference }: { reference: string }) {
  const url = httpUrl(reference);
  return (
    <p className="mt-1.5 text-xs text-fg-subtle">
      Authority:{" "}
      {url ? (
        <a
          href={url.href}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 text-accent hover:underline"
        >
          {url.hostname}
          <ExternalLink aria-hidden className="size-3" />
        </a>
      ) : (
        <span className="font-mono">{reference}</span>
      )}
    </p>
  );
}

/**
 * The empty state is a worked example, not "no findings yet".
 *
 * A superlative with no substantiation is the single most common thing the
 * register catches, so the first thing somebody sees on this screen is the tool
 * doing its job rather than an empty box asking them to think of something.
 */
const SAMPLE = "The best SDS software on the market — guaranteed 40% faster";
