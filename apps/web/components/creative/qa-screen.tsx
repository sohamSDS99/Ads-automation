"use client";

import { ArrowLeft, Package } from "lucide-react";
import Link from "next/link";
import { useMemo, useState, type ReactNode } from "react";

import { ExceptionList } from "@/components/creative/exception-list";
import { LaunchMinimums } from "@/components/creative/launch-minimums";
import { PreviewGrid } from "@/components/creative/preview-grid";
import { SpecConformanceTable } from "@/components/creative/spec-conformance-table";
import { Alert } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api";
import type { ConformanceFilter } from "@/lib/api/creative-packages";
import {
  useConformance,
  useCreativeAssets,
  useCreativeExceptions,
  useRenderPreviews,
  useRunPackage,
  useUsers,
} from "@/lib/queries";

function notYet(error: unknown): string | null {
  return error instanceof ApiError && error.status === 404 ? error.message : null;
}

function Section({ id, title, meta, children }: { id: string; title: string; meta?: ReactNode; children: ReactNode }) {
  return (
    <section aria-labelledby={id} className="flex min-w-0 flex-col gap-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-b pb-2">
        <h2 id={id} className="text-md font-medium tracking-tight text-fg">
          {title}
        </h2>
        {meta}
      </div>
      {children}
    </section>
  );
}

function Failed({ what, error }: { what: string; error: unknown }) {
  return (
    <Alert tone="error" title={`${what} could not be loaded`}>
      {error instanceof ApiError ? error.message : "Refresh the page to try again."}
    </Alert>
  );
}

/**
 * The QA screen (Stage 04 PRD §15.3 `…/runs/[runId]/qa`, §15.4 K): 4.6.4's
 * previews at true scale, 4.6.1's conformance, the run's exceptions and each
 * campaign's launch minimums. Read-only for every role and all of it the
 * server's — a verdict here is a verdict a node recorded.
 */
export function QaScreen({ projectId, runId }: { projectId: string; runId: string }) {
  const [filter, setFilter] = useState<ConformanceFilter>("all");
  const previews = useRenderPreviews(runId);
  const conformance = useConformance(runId, filter);
  const exceptions = useCreativeExceptions(runId);
  const pkg = useRunPackage(runId);
  const assets = useCreativeAssets(runId);
  const users = useUsers();

  const assetName = useMemo(() => {
    const byId = new Map(
      (assets.data?.items ?? []).map((asset) => {
        const kind = asset.kind.replace(/_/g, " ");
        const text = asset.text ? ` “${asset.text}”` : "";
        return [asset.id, `${kind.charAt(0).toUpperCase()}${kind.slice(1)}${text}`] as const;
      }),
    );
    return (assetId: string) => byId.get(assetId) ?? null;
  }, [assets.data]);
  const nameOf = useMemo(() => {
    const byId = new Map((users.data?.users ?? []).map((user) => [user.id, user.name] as const));
    return (userId: string) => byId.get(userId) ?? null;
  }, [users.data]);

  const home = `/projects/${projectId}/creative/runs/${runId}`;

  return (
    <div className="flex flex-col gap-8">
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <Link
            href={home}
            className="inline-flex w-fit items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
          >
            <ArrowLeft className="size-4 shrink-0" aria-hidden />
            Back to the Creative Console
          </Link>
          <Link href={`${home}/package`} className="inline-flex items-center gap-1.5 text-sm text-accent hover:underline">
            <Package className="size-4" aria-hidden />
            Package and release
          </Link>
        </div>
        <div>
          <h1 className="text-lg font-medium tracking-tight text-fg">QA</h1>
          <p className="text-sm text-fg-muted">
            What the final checks recorded: each ad as Google would draw it, every measured constraint, the legal
            exceptions and each campaign’s launch minimum.
          </p>
        </div>
      </div>

      <Section id="qa-minimums" title="Launch minimums">
        {pkg.isPending ? (
          <Skeleton className="h-24 w-full" />
        ) : pkg.error ? (
          notYet(pkg.error) ? (
            <p className="text-sm text-fg-muted">
              Counted when node 4.7.1 assembles the package, from the final pin’s spec sheet. {notYet(pkg.error)}
            </p>
          ) : (
            <Failed what="The launch minimums" error={pkg.error} />
          )
        ) : (
          <LaunchMinimums campaigns={pkg.data.package.campaigns} />
        )}
      </Section>

      <Section id="qa-previews" title="Ad previews">
        {previews.isPending ? (
          <div className="flex flex-wrap gap-6">
            <Skeleton className="h-48 w-serp-mobile" />
            <Skeleton className="h-36 w-serp-desktop max-w-full" />
          </div>
        ) : previews.error ? (
          <Failed what="The previews" error={previews.error} />
        ) : previews.data.items.length === 0 ? (
          <p className="text-sm text-fg-muted" data-testid="previews-empty">
            No previews yet. Node 4.6.4 renders every RSA’s likeliest combinations and its longest on a phone and a
            desktop once the final lint runs. Follow it in the{" "}
            <Link href={home} className="text-accent hover:underline">
              Creative Console
            </Link>
            .
          </p>
        ) : (
          <PreviewGrid previews={previews.data.items} />
        )}
      </Section>

      <Section id="qa-conformance" title="Spec conformance">
        {conformance.isPending ? (
          <Skeleton className="h-64 w-full" />
        ) : conformance.error ? (
          notYet(conformance.error) ? (
            <p className="text-sm text-fg-muted">{notYet(conformance.error)}</p>
          ) : (
            <Failed what="Spec conformance" error={conformance.error} />
          )
        ) : (
          <SpecConformanceTable
            data={conformance.data}
            filter={filter}
            onFilter={setFilter}
            assetName={assetName}
            loading={conformance.isFetching && conformance.isPlaceholderData}
          />
        )}
      </Section>

      <Section id="qa-exceptions" title="Exceptions">
        {exceptions.isPending ? (
          <Skeleton className="h-24 w-full" />
        ) : exceptions.error ? (
          <Failed what="The exceptions" error={exceptions.error} />
        ) : (
          <ExceptionList set={exceptions.data} nameOf={nameOf} />
        )}
      </Section>
    </div>
  );
}
