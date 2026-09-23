"use client";

import { Check, FileUp, Loader2, Lock, Paperclip, TriangleAlert, X } from "lucide-react";
import { useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ApiError } from "@/lib/api";
import { reauth } from "@/lib/api/claims";
import {
  requiredArtifacts,
  submitHumanTask,
  uploadTaskAttachment,
  type HumanTask,
} from "@/lib/api/tasks";
import { absoluteTime, relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";

type Upload = { name: string; state: "uploading" | "done" | "failed"; detail?: string };

/**
 * A person-task card (PRD §15.3 D).
 *
 * The rule that shapes this whole component: **for anyone who is not the
 * assignee, every control is absent — not disabled** — and the card reads
 * `Assigned to {name}`. A disabled submit button on somebody else's legal
 * attestation is an invitation to go looking for the state that enables it,
 * and there isn't one. `can_submit` is the server's answer; this file never
 * re-derives it.
 */
export function TaskCard({
  task,
  onDone,
  className,
}: {
  task: HumanTask;
  onDone: () => void;
  className?: string;
}) {
  const required = requiredArtifacts(task);
  const [confirmed, setConfirmed] = useState<string[]>([]);
  const [reference, setReference] = useState("");
  const [note, setNote] = useState("");
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [attached, setAttached] = useState(task.attachment_paths.length);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [asking, setAsking] = useState(false);
  const password = useRef("");
  const passwordField = useRef<HTMLInputElement>(null);

  const done = task.status === "completed";
  const outstanding = required.filter((key) => !confirmed.includes(key));
  const ready = outstanding.length === 0 && (required.length === 0 || attached > 0);

  const upload = async (files: FileList | null) => {
    if (!files) return;
    for (const file of Array.from(files)) {
      // Per-file state, not one aggregate bar: five files where one failed must
      // not read as "uploading 80%".
      setUploads((current) => [...current, { name: file.name, state: "uploading" }]);
      try {
        await uploadTaskAttachment(task.id, file);
        setAttached((count) => count + 1);
        setUploads((current) =>
          current.map((item) =>
            item.name === file.name ? { ...item, state: "done" } : item,
          ),
        );
      } catch (caught) {
        setUploads((current) =>
          current.map((item) =>
            item.name === file.name
              ? {
                  ...item,
                  state: "failed",
                  detail: caught instanceof ApiError ? caught.detail : "Upload failed",
                }
              : item,
          ),
        );
      }
    }
  };

  const submit = async () => {
    if (!password.current) {
      setError("Enter your password. An attestation records that you were present.");
      passwordField.current?.focus();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const { token } = await reauth(password.current);
      await submitHumanTask(task.id, {
        payload: note.trim() ? { note: note.trim() } : {},
        artifacts_confirmed: confirmed,
        reference: reference.trim() || undefined,
        reauthToken: token,
      });
      password.current = "";
      if (passwordField.current) passwordField.current.value = "";
      onDone();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : "That did not go through.");
      password.current = "";
      if (passwordField.current) passwordField.current.value = "";
    } finally {
      setBusy(false);
    }
  };

  return (
    <article
      className={cn(
        "rounded-[var(--radius)] border bg-surface-raised",
        done && "opacity-80",
        className,
      )}
    >
      <header className="flex flex-wrap items-start justify-between gap-3 border-b px-5 py-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-[length:var(--text-md)] font-medium text-fg">{task.title}</h3>
            <Badge tone={task.blocking_for === "publish" ? "warning" : "neutral"}>
              {task.blocking_for === "publish" ? "Blocks publish" : "Blocks launch"}
            </Badge>
            <Badge>{task.task_key}</Badge>
          </div>
          <p className="mt-1 text-sm text-fg-muted">
            {task.project_name ? `${task.project_name} · ` : null}
            opened {relativeTime(task.created_at)}
            {task.due_at ? ` · due ${absoluteTime(task.due_at)}` : null}
          </p>
        </div>
        <div className="text-right">
          <p className="text-xs text-fg-subtle">Assigned to</p>
          <p className="text-sm font-medium text-fg">
            {task.assignee_name ?? task.assignee_email ?? "somebody"}
          </p>
        </div>
      </header>

      <div className="space-y-4 px-5 py-4">
        <p className="text-sm leading-relaxed text-fg">{task.instructions}</p>

        {done ? (
          <p className="flex items-center gap-2 text-sm text-status-success">
            <Check aria-hidden className="size-4" />
            Attested {task.completed_at ? relativeTime(task.completed_at) : ""}
          </p>
        ) : task.can_submit ? (
          <>
            {required.length > 0 ? (
              <fieldset>
                <legend className="text-xs font-medium text-fg-subtle">Required artifacts</legend>
                <ul className="mt-2 space-y-1.5">
                  {required.map((key) => (
                    <li key={key}>
                      <label className="flex items-center gap-2 text-sm text-fg">
                        <input
                          type="checkbox"
                          className="size-4 accent-[var(--accent)]"
                          checked={confirmed.includes(key)}
                          onChange={(event) =>
                            setConfirmed((current) =>
                              event.target.checked
                                ? [...current, key]
                                : current.filter((item) => item !== key),
                            )
                          }
                        />
                        {key}
                      </label>
                    </li>
                  ))}
                </ul>
              </fieldset>
            ) : null}

            <div>
              <label className="text-xs font-medium text-fg-subtle" htmlFor={`file-${task.id}`}>
                Supporting documents
              </label>
              <div className="mt-1.5 flex items-center gap-2">
                <input
                  id={`file-${task.id}`}
                  type="file"
                  multiple
                  className="sr-only"
                  onChange={(event) => void upload(event.target.files)}
                />
                <label
                  htmlFor={`file-${task.id}`}
                  className="inline-flex h-8 cursor-pointer items-center gap-2 rounded-[var(--radius)] border bg-surface-raised px-3 text-sm text-fg transition-colors hover:bg-surface-hover"
                >
                  <FileUp aria-hidden className="size-4" />
                  Choose files
                </label>
                <span data-numeric className="text-xs text-fg-subtle">
                  <Paperclip aria-hidden className="mr-1 inline size-3" />
                  {attached} attached
                </span>
              </div>
              {uploads.length > 0 ? (
                <ul className="mt-2 space-y-1" aria-live="polite">
                  {uploads.map((item) => (
                    <li key={item.name} className="flex items-center gap-2 text-xs">
                      {item.state === "uploading" ? (
                        <Loader2 aria-hidden className="size-3 animate-spin text-fg-subtle" />
                      ) : item.state === "done" ? (
                        <Check aria-hidden className="size-3 text-status-success" />
                      ) : (
                        <X aria-hidden className="size-3 text-status-failed" />
                      )}
                      <span className="truncate text-fg-muted">{item.name}</span>
                      {item.detail ? (
                        <span className="text-status-failed">{item.detail}</span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="flex flex-col gap-1.5">
                <label className="text-xs font-medium text-fg-subtle" htmlFor={`ref-${task.id}`}>
                  Reference
                </label>
                <Input
                  id={`ref-${task.id}`}
                  value={reference}
                  onChange={(event) => setReference(event.target.value)}
                  placeholder="Certificate or ticket number"
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <label className="text-xs font-medium text-fg-subtle" htmlFor={`note-${task.id}`}>
                  Note
                </label>
                <Textarea
                  id={`note-${task.id}`}
                  rows={1}
                  value={note}
                  onChange={(event) => setNote(event.target.value)}
                />
              </div>
            </div>

            {asking ? (
              <div className="rounded-[var(--radius)] border border-accent bg-accent-soft p-4">
                <label
                  className="text-sm font-medium text-fg"
                  htmlFor={`stepup-${task.id}`}
                >
                  Confirm your password to attest
                </label>
                <p className="mt-0.5 mb-2 text-xs text-fg-muted">
                  This records that you personally performed this act. It is not delegable and
                  an administrator cannot do it for you.
                </p>
                <Input
                  id={`stepup-${task.id}`}
                  ref={passwordField}
                  type="password"
                  autoComplete="off"
                  data-1p-ignore
                  data-lpignore="true"
                  name={`attest-proof-${task.id}`}
                  disabled={busy}
                  onChange={(event) => {
                    password.current = event.target.value;
                    if (error) setError(null);
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !busy) void submit();
                  }}
                />
              </div>
            ) : null}

            {error ? (
              <p className="flex items-start gap-1.5 text-sm text-status-failed" aria-live="polite">
                <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
                {error}
              </p>
            ) : null}

            <div className="flex items-center justify-between gap-3">
              <p className="text-xs text-fg-subtle">
                {outstanding.length > 0
                  ? `${outstanding.length} artifact${outstanding.length === 1 ? "" : "s"} still to confirm`
                  : required.length > 0 && attached === 0
                    ? "Attach the documents this rests on"
                    : "Ready to attest"}
              </p>
              <Button
                disabled={!ready || busy}
                onClick={() => (asking ? void submit() : setAsking(true))}
              >
                {busy ? <Loader2 aria-hidden className="size-4 animate-spin" /> : null}
                {asking ? "Attest and submit" : "Submit attestation"}
              </Button>
            </div>
          </>
        ) : (
          // Not disabled. Absent — and a sentence saying whose it is.
          <p className="flex items-center gap-2 rounded-[var(--radius)] border bg-surface px-3 py-2 text-sm text-fg-muted">
            <Lock aria-hidden className="size-4 shrink-0 text-fg-subtle" />
            Assigned to {task.assignee_name ?? task.assignee_email ?? "somebody else"}. Only they
            can perform it.
          </p>
        )}
      </div>
    </article>
  );
}
