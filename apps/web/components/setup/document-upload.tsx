"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Trash2, TriangleAlert, Upload } from "lucide-react";
import { useRef, useState } from "react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import {
  deleteDocument,
  documentScale,
  fileSize,
  listDocuments,
  uploadDocument,
  type ProjectDocument,
} from "@/lib/api/documents";
import { keys } from "@/lib/queries";

/** Fallback while the server's own list is in flight. */
const ACCEPT = [".pdf", ".docx", ".csv", ".tsv", ".txt", ".md"];

/**
 * The other half of the business context: the documents the brand already has.
 *
 * Six text boxes cannot hold a pricing sheet or a positioning deck, and asking
 * someone to paste one in loses the structure with the formatting. A file
 * dropped here is read on the server, split into passages, and stored as
 * evidence the research nodes can cite — so what a node says about the pricing
 * tiers points back at the page of the PDF it came from.
 *
 * Every number this renders is the server's: how much text came out, how many
 * passages it became, what could not be read. A filename and a green tick
 * would look identical for a real document and for a scan with no text layer,
 * and those two cases produce very different reports.
 */
export function DocumentUpload({ projectId, disabled }: { projectId: string; disabled: boolean }) {
  const queryClient = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<ProjectDocument | null>(null);

  const library = useQuery({
    queryKey: keys.documents(projectId),
    queryFn: () => listDocuments(projectId),
  });

  const upload = useMutation({
    mutationFn: (file: File) => uploadDocument(projectId, file),
    onSuccess: async (result) => {
      setError(null);
      const { document } = result;
      const note = document.warnings[0];
      toast.success(`${document.filename} added`, {
        description: note ?? `${documentScale(document)} the run can now cite.`,
      });
      await queryClient.invalidateQueries({ queryKey: keys.documents(projectId) });
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.detail : "That file could not be uploaded."),
  });

  const remove = useMutation({
    mutationFn: (document: ProjectDocument) => deleteDocument(projectId, document.id),
    onSuccess: async (_result, document) => {
      toast.success(`${document.filename} removed`, {
        description: "Its passages are gone from this project's evidence too.",
      });
      await queryClient.invalidateQueries({ queryKey: keys.documents(projectId) });
    },
    onError: (err) =>
      toast.error("Not removed", {
        description: err instanceof ApiError ? err.detail : "Try again in a moment.",
      }),
  });

  const documents = library.data?.documents ?? [];
  const accepted = library.data?.accepted_extensions ?? ACCEPT;
  const maxDocuments = library.data?.max_documents ?? 0;
  const full = maxDocuments > 0 && documents.length >= maxDocuments;
  const busy = upload.isPending;

  /** One at a time: each file is read, split and embedded server-side. */
  async function send(files: FileList | File[]) {
    setError(null);
    for (const file of Array.from(files)) {
      try {
        await upload.mutateAsync(file);
      } catch {
        // `onError` has already put the reason on screen. Stopping here rather
        // than carrying on means the message still refers to the file the
        // person is looking at.
        break;
      }
    }
    if (fileInput.current) fileInput.current.value = "";
  }

  return (
    <fieldset className="space-y-3">
      <legend className="text-sm font-medium text-fg">Background documents</legend>
      <p className="-mt-1 text-xs text-fg-muted">
        Optional. A pricing sheet, a positioning one-pager, an objection-handling doc — anything
        that says more about this brand than the boxes above can hold. The text is read out of the
        file and every research node can quote it, with a citation back to the page it came from.
      </p>

      <input
        ref={fileInput}
        type="file"
        multiple
        accept={accepted.join(",")}
        disabled={disabled || busy || full}
        className="sr-only"
        id="context-documents"
        onChange={(event) => {
          const chosen = event.target.files;
          if (chosen?.length) void send(chosen);
        }}
      />

      {disabled ? null : (
        <div
          onDragOver={(event) => {
            event.preventDefault();
            if (!busy && !full) setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            if (busy || full) return;
            if (event.dataTransfer.files.length) void send(event.dataTransfer.files);
          }}
          className={[
            "flex flex-col items-center gap-2 rounded-[var(--radius)] border border-dashed px-4 py-6 text-center transition-colors",
            dragging ? "border-border-strong bg-surface-hover" : "border-border",
          ].join(" ")}
        >
          <Button
            type="button"
            variant="secondary"
            size="sm"
            disabled={busy || full}
            onClick={() => fileInput.current?.click()}
          >
            {busy ? <Spinner label="Reading" /> : <Upload aria-hidden />}
            {busy ? "Reading the file…" : "Add documents"}
          </Button>
          <p className="text-xs text-fg-subtle">
            {full
              ? `This project already holds ${maxDocuments} documents, which is the limit.`
              : `Drag files here. ${accepted.join(", ")}, up to ${fileSize(
                  library.data?.max_bytes ?? 0,
                )} each.`}
          </p>
        </div>
      )}

      {error ? (
        <Alert tone="error" title="That file was not added">
          {error}
        </Alert>
      ) : null}

      {library.isError ? (
        <Alert tone="error" title="The document list could not be loaded">
          Reload the page to try again.
        </Alert>
      ) : null}

      {documents.length ? (
        <ul className="space-y-2">
          {documents.map((document) => (
            <li
              key={document.id}
              className="flex items-start gap-3 rounded-[var(--radius)] border bg-surface-raised px-3.5 py-3"
            >
              <FileText className="mt-0.5 size-4 shrink-0 text-fg-muted" aria-hidden />
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-fg">{document.filename}</p>
                <p className="text-xs text-fg-muted">
                  {fileSize(document.bytes)} · {documentScale(document)}
                </p>
                {document.preview ? (
                  <p className="mt-1.5 line-clamp-2 text-xs text-fg-subtle">{document.preview}</p>
                ) : null}
                {document.warnings.map((warning) => (
                  <p
                    key={warning}
                    className="mt-1.5 flex items-start gap-1.5 text-xs text-fg-muted"
                  >
                    <TriangleAlert
                      className="mt-0.5 size-3.5 shrink-0 text-status-gate"
                      aria-hidden
                    />
                    {warning}
                  </p>
                ))}
              </div>
              {disabled ? null : (
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label={`Remove ${document.filename}`}
                  disabled={remove.isPending}
                  onClick={() => setPending(document)}
                >
                  <Trash2 aria-hidden />
                </Button>
              )}
            </li>
          ))}
        </ul>
      ) : null}

      {documents.length && maxDocuments ? (
        <p className="text-xs text-fg-subtle">
          {documents.length} of {maxDocuments} documents ·{" "}
          {(library.data?.total_chars ?? 0).toLocaleString()} characters of context
        </p>
      ) : null}

      <ConfirmDialog
        open={pending !== null}
        onOpenChange={(open) => {
          if (!open) setPending(null);
        }}
        title="Remove this document?"
        description={pending?.filename}
        body={
          pending ? (
            <p className="text-sm text-fg-muted">
              Its {pending.passage_count} passage
              {pending.passage_count === 1 ? "" : "s"} are deleted from this project&apos;s
              evidence. Reports that already cite them keep the text they quoted; new runs will
              not see this document at all.
            </p>
          ) : null
        }
        confirmLabel="Remove"
        destructive
        onConfirm={async () => {
          if (pending) await remove.mutateAsync(pending);
          setPending(null);
        }}
      />
    </fieldset>
  );
}
