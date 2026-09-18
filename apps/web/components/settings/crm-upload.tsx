"use client";

import { useMutation } from "@tanstack/react-query";
import { FileUp, TriangleAlert, Upload } from "lucide-react";
import { useRef, useState } from "react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import {
  fieldLabel,
  previewCsv,
  uploadCsv,
  type CsvIngest,
  type CsvPreview,
} from "@/lib/api/sources";

const IGNORE = "";

/**
 * CRM export → evidence, one column at a time.
 *
 * Two passes on purpose (PRD §9.5): the file is posted once to read its headers
 * and propose a mapping, and again once a person has confirmed it. The preview
 * writes nothing, so uploading the wrong export costs a round trip and no data.
 */
export function CrmUpload({ projectId, disabled }: { projectId: string; disabled: boolean }) {
  const fileInput = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<CsvPreview | null>(null);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [outcome, setOutcome] = useState<"won" | "lost">("won");
  const [result, setResult] = useState<CsvIngest | null>(null);
  const [error, setError] = useState<string | null>(null);

  const inspect = useMutation({
    mutationFn: (chosen: File) => previewCsv(projectId, chosen),
    onSuccess: (data) => {
      setPreview(data);
      setResult(null);
      setError(null);
      // Start from what the server proposed rather than from nothing: most
      // exports map cleanly and the person is confirming, not authoring.
      setMapping(
        Object.fromEntries(
          data.columns
            .filter((column) => column.suggested_field)
            .map((column) => [column.header, column.suggested_field as string]),
        ),
      );
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.detail : "That file could not be read."),
  });

  const ingest = useMutation({
    mutationFn: () =>
      uploadCsv(
        projectId,
        file as File,
        outcome,
        Object.fromEntries(Object.entries(mapping).filter(([, value]) => value !== IGNORE)),
      ),
    onSuccess: (data) => {
      setResult(data);
      setPreview(null);
      setFile(null);
      if (fileInput.current) fileInput.current.value = "";
      toast.success(`${data.rows_accepted} rows imported`, {
        description: `${data.evidence_written} new evidence rows, ${data.duplicates} already known.`,
      });
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.detail : "The import could not be completed."),
  });

  const mapped = new Set(Object.values(mapping).filter((value) => value !== IGNORE));
  const missing = (preview?.required_fields ?? []).filter((field) => !mapped.has(field));

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <input
          ref={fileInput}
          type="file"
          accept=".csv,text/csv"
          disabled={disabled}
          className="sr-only"
          id="csv-file"
          onChange={(event) => {
            const chosen = event.target.files?.[0] ?? null;
            setFile(chosen);
            if (chosen) inspect.mutate(chosen);
          }}
        />
        <Button
          type="button"
          variant="secondary"
          size="sm"
          disabled={disabled || inspect.isPending}
          onClick={() => fileInput.current?.click()}
        >
          {inspect.isPending ? <Spinner label="Reading" /> : <FileUp aria-hidden />}
          Choose a CSV
        </Button>
        {file ? (
          <span className="text-sm text-fg-muted">
            {file.name}
            {preview ? ` · ${preview.row_count} rows` : ""}
          </span>
        ) : (
          <span className="text-sm text-fg-subtle">
            A closed-won or closed-lost export from the CRM.
          </span>
        )}
      </div>

      {error ? (
        <Alert tone="error" title="That did not work">
          {error}
        </Alert>
      ) : null}

      {result ? (
        <Alert tone={result.errors.length ? "warning" : "info"} title="Import finished">
          <p>
            {result.rows_accepted} rows accepted, {result.rows_skipped} skipped.{" "}
            {result.evidence_written} written as evidence, {result.duplicates} already known.
          </p>
          {result.errors.length ? (
            <ul className="mt-2 space-y-0.5 text-xs">
              {result.errors.slice(0, 5).map((rowError, index) => (
                <li key={index}>
                  Row {rowError.row}
                  {rowError.column ? ` · ${rowError.column}` : ""}: {rowError.problem}
                </li>
              ))}
              {result.errors.length > 5 ? (
                <li className="text-fg-subtle">and {result.errors.length - 5} more</li>
              ) : null}
            </ul>
          ) : null}
        </Alert>
      ) : null}

      {preview ? (
        <div className="space-y-3 rounded-[var(--radius)] border bg-surface-raised">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b px-4 py-3">
            <div>
              <h4 className="font-medium text-fg">Map the columns</h4>
              <p className="text-sm text-fg-muted">
                Anything left on “Ignore” is not imported.
              </p>
            </div>
            <label className="flex items-center gap-2 text-sm text-fg-muted">
              These rows are
              <Select
                value={outcome}
                aria-label="Deal outcome"
                onChange={(event) => setOutcome(event.target.value as "won" | "lost")}
                className="w-36"
              >
                <option value="won">closed won</option>
                <option value="lost">closed lost</option>
              </Select>
            </label>
          </div>

          <div className="px-1">
            <Table label="Column mapping">
              <thead>
                <tr>
                  <Th>Column in your file</Th>
                  <Th>Sample values</Th>
                  <Th>Import as</Th>
                </tr>
              </thead>
              <tbody>
                {preview.columns.map((column) => (
                  <Tr key={column.header}>
                    <Td className="font-mono text-xs">{column.header}</Td>
                    <Td className="max-w-72 truncate text-fg-muted">
                      {column.values.join(" · ") || "—"}
                    </Td>
                    <Td>
                      <Select
                        value={mapping[column.header] ?? IGNORE}
                        aria-label={`Import ${column.header} as`}
                        onChange={(event) =>
                          setMapping((current) => ({
                            ...current,
                            [column.header]: event.target.value,
                          }))
                        }
                        className="w-52"
                      >
                        <option value={IGNORE}>Ignore</option>
                        {preview.canonical_fields.map((field) => (
                          <option
                            key={field}
                            value={field}
                            disabled={
                              mapped.has(field) && mapping[column.header] !== field
                            }
                          >
                            {fieldLabel(field)}
                            {preview.required_fields.includes(field) ? " (required)" : ""}
                          </option>
                        ))}
                      </Select>
                    </Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          </div>

          <div className="flex flex-wrap items-center justify-between gap-3 border-t px-4 py-3">
            {missing.length ? (
              <p className="flex items-center gap-2 text-sm text-fg-muted">
                <TriangleAlert className="size-4 shrink-0 text-status-gate" aria-hidden />
                Still to map: {missing.map(fieldLabel).join(", ")}
              </p>
            ) : (
              <p className="text-sm text-fg-muted">Every required field is mapped.</p>
            )}
            <Button
              type="button"
              size="sm"
              disabled={ingest.isPending || missing.length > 0}
              onClick={() => ingest.mutate()}
            >
              {ingest.isPending ? <Spinner label="Importing" /> : <Upload aria-hidden />}
              Import {preview.row_count} rows
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
