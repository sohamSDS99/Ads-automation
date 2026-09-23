"use client";

import * as DialogPrimitive from "@radix-ui/react-dialog";
import { CheckCircle2, CircleDashed, Lock, TriangleAlert, XCircle } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import type { ContentGuidelinePayload, GuidelineDetail } from "@/lib/api/guidelines";
import { absoluteTime } from "@/lib/format";
import { usePublishGuideline } from "@/lib/queries";

/**
 * Publish, and everything a person should know before they do (PRD §15.3 H).
 *
 * Three things make this dialog worth its size.
 *
 * **It shows the state before it asks for the decision.** G5, G6, H1, H2 and
 * the critique are listed with who decided and when, *above* the confirm
 * field. A dialog that asks first and explains on failure turns a five-minute
 * fix into five round trips through a legal owner's inbox.
 *
 * **A refusal carries every blocker.** `publish_guideline` raises with the
 * whole list and the route serialises all of it — so this renders all of it,
 * each with its own fix link, and stays open. It never re-derives a blocker:
 * the server decides what stops a publish, and a second opinion in TypeScript
 * would eventually disagree with the first.
 *
 * **It says what publishing costs.** The payload becomes immutable and the
 * ruleset it mints is frozen by a database trigger. Typing the version is the
 * friction that matches that: this is not an action with an undo.
 */
export function PublishDialog({
  guideline,
  projectId,
  open,
  onOpenChange,
}: {
  guideline: GuidelineDetail;
  projectId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const publish = usePublishGuideline(guideline.id, projectId);
  const [typed, setTyped] = useState("");
  const [blockers, setBlockers] = useState<NonNullable<ApiError["problem"]>["blockers"]>(undefined);

  const payload = guideline.payload;
  // The MAJOR the server will mint. A draft carries a provisional version that
  // publish re-mints, so this is a prediction — which is exactly why the API
  // takes `confirm_version` and answers `409 version_conflict` naming what it
  // expected. The dialog does not try to be cleverer than that exchange.
  const expected = guideline.version_major || 1;
  const expectedLabel = `v${expected}.0`;
  const confirmed = typed.trim().toLowerCase() === expectedLabel.toLowerCase();

  async function run() {
    setBlockers(undefined);
    try {
      const receipt = await publish.mutateAsync(expected);
      toast.success(`Published ${receipt.version}`, {
        description: receipt.ruleset_version
          ? `Ruleset ${receipt.ruleset_version} · ${receipt.rule_count} rules. Stage 04 reads it from now on.`
          : undefined,
        duration: 20_000,
      });
      onOpenChange(false);
      setTyped("");
    } catch (error) {
      if (error instanceof ApiError && error.problem?.blockers?.length) {
        setBlockers(error.problem.blockers);
        return;
      }
      if (error instanceof ApiError && error.status === 409) {
        // `version_conflict` — somebody published in between. The server names
        // what it expected; re-reading is the only correct next move.
        toast.error("The version moved under you", {
          description: `${error.detail} Re-read the rulebook before publishing again.`,
          duration: 20_000,
        });
        return;
      }
      toast.error("It could not be published", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
        duration: 20_000,
      });
    }
  }

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogContent title={`Publish ${expectedLabel}`}>
        <DialogBody className="flex flex-col gap-4">
          <Gates payload={payload} />
          <Counts payload={payload} />

          {blockers?.length ? (
            <div className="rounded-[var(--radius)] border border-[var(--status-failed)]/40 bg-danger-soft p-3">
              <p className="text-sm font-medium text-danger">
                {blockers.length === 1
                  ? "One thing is outstanding"
                  : `${blockers.length} things are outstanding`}
              </p>
              <ul className="mt-2 flex flex-col gap-2">
                {blockers.map((blocker) => (
                  <li key={blocker.code} className="text-sm text-fg">
                    {blocker.detail}{" "}
                    <Link href={blocker.fix_url} className="text-accent underline underline-offset-2">
                      Open
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          <div className="rounded-[var(--radius)] bg-surface-sunken p-3 text-sm text-fg-muted">
            <p className="flex items-start gap-2">
              <Lock className="mt-0.5 size-4 shrink-0" aria-hidden />
              <span>
                Publishing freezes this payload and mints an immutable ruleset. Nothing edits a
                published version — a change is a new one. Stage 04 starts reading it immediately.
              </span>
            </p>
          </div>

          <label className="flex flex-col gap-1.5 text-sm">
            <span className="text-fg">
              Type <span className="font-mono text-fg">{expectedLabel}</span> to confirm
            </span>
            <Input
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              placeholder={expectedLabel}
              autoComplete="off"
              spellCheck={false}
            />
          </label>
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={() => void run()} disabled={!confirmed || publish.isPending}>
            {publish.isPending ? "Publishing…" : `Publish ${expectedLabel}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </DialogPrimitive.Root>
  );
}

/** G5, G6, H1, H2 and the critique — the five things that gate a publish. */
function Gates({ payload }: { payload: ContentGuidelinePayload | null }) {
  const decisions = payload?.decisions ?? [];
  const signatures = payload?.signatures ?? [];
  const tasks = payload?.human_tasks ?? [];
  const blocking = (payload?.critique_issues ?? []).filter((issue) => issue.severity === "blocking");

  const g5 = decisions.find((decision) => decision.gate_key === "G5");
  const g6 = decisions.find((decision) => decision.gate_key === "G6");
  const h1 = tasks.find((task) => task.task_key === "H1");
  const h2 = tasks.find((task) => task.task_key === "H2");
  const signature = signatures.find((item) => !item.voided_at);

  return (
    <ul className="divide-y rounded-[var(--radius)] border">
      <GateRow
        label="G5 · Visual identity"
        state={g5?.status === "approved" ? "done" : g5?.status === "rejected" ? "bad" : "waiting"}
        detail={
          g5?.decided_at
            ? `${g5.status} ${absoluteTime(g5.decided_at)}`
            : "No decision recorded yet"
        }
      />
      <GateRow
        label="G6 · Sign-off matrix"
        state={g6?.status === "approved" ? "done" : g6?.status === "rejected" ? "bad" : "waiting"}
        detail={
          g6?.decided_at
            ? `${g6.status} ${absoluteTime(g6.decided_at)}`
            : "No decision recorded yet"
        }
      />
      <GateRow
        label="H1 · Legal claim signature"
        state={signature ? "done" : h1?.status === "completed" ? "done" : "waiting"}
        detail={
          signature
            ? `${signature.claim_count ?? 0} claims · set ${(signature.set_hash ?? "").slice(0, 12)}${
                signature.expires_at ? ` · expires ${absoluteTime(signature.expires_at)}` : ""
              }`
            : "Blocks publish — no unvoided signature on the register"
        }
      />
      <GateRow
        label="H2 · Verification attestation"
        // H2 blocks launch, not publish. An outstanding H2 is stated and is
        // deliberately not styled as a failure here: a company can hold a
        // complete rulebook before it is verified to advertise.
        state={h2 ? (h2.status === "completed" ? "done" : "note") : "done"}
        detail={
          h2
            ? h2.status === "completed"
              ? "Submitted"
              : "Blocks launch, not publish"
            : "Nothing required"
        }
      />
      <GateRow
        label="Its own review"
        state={blocking.length === 0 ? "done" : "bad"}
        detail={
          blocking.length === 0
            ? "No blocking findings"
            : `${blocking.length} blocking ${blocking.length === 1 ? "finding" : "findings"}`
        }
      />
    </ul>
  );
}

function GateRow({
  label,
  state,
  detail,
}: {
  label: string;
  state: "done" | "waiting" | "bad" | "note";
  detail: string;
}) {
  const Icon =
    state === "done"
      ? CheckCircle2
      : state === "bad"
        ? XCircle
        : state === "note"
          ? TriangleAlert
          : CircleDashed;
  const colour =
    state === "done"
      ? "text-[var(--status-success)]"
      : state === "bad"
        ? "text-[var(--status-failed)]"
        : state === "note"
          ? "text-[var(--status-gate)]"
          : "text-fg-subtle";
  return (
    <li className="flex items-start gap-3 p-3">
      <Icon className={`mt-0.5 size-4 shrink-0 ${colour}`} aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-fg">{label}</p>
        <p className="text-sm text-fg-muted">{detail}</p>
      </div>
    </li>
  );
}

/** What is about to be frozen, by the numbers. */
function Counts({ payload }: { payload: ContentGuidelinePayload | null }) {
  const rules = payload?.rules ?? [];
  if (rules.length === 0) {
    return (
      <p className="text-sm text-fg-muted">
        This version has no compiled rules. Publishing it would give Stage 04 an empty ruleset.
      </p>
    );
  }
  const bySeverity = new Map<string, number>();
  const byCategory = new Map<string, number>();
  for (const rule of rules) {
    bySeverity.set(rule.severity, (bySeverity.get(rule.severity) ?? 0) + 1);
    byCategory.set(rule.category, (byCategory.get(rule.category) ?? 0) + 1);
  }
  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm text-fg">
        {rules.length} rules across {byCategory.size}{" "}
        {byCategory.size === 1 ? "category" : "categories"}
      </p>
      <div className="flex flex-wrap gap-2">
        {["blocking", "warning", "advisory"].map((severity) =>
          bySeverity.get(severity) ? (
            <Badge
              key={severity}
              tone={severity === "blocking" ? "danger" : severity === "warning" ? "warning" : "neutral"}
            >
              {bySeverity.get(severity)} {severity}
            </Badge>
          ) : null,
        )}
      </div>
    </div>
  );
}
