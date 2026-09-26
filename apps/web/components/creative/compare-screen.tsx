"use client";

import { ArrowLeft, GitCompareArrows } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { PackageDiff } from "@/components/creative/package-diff";
import { PACKAGE_STATUS } from "@/components/creative/package-screen";
import { Alert } from "@/components/ui/alert";
import { EmptyState } from "@/components/ui/empty-state";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api";
import type { CreativePackageSummary } from "@/lib/api/creative";
import { compareHref, packageHref } from "@/lib/api/creative-packages";
import { middle } from "@/lib/creative/package";
import { shortDate } from "@/lib/format";
import { useCreativeOverview, usePackageDiff } from "@/lib/queries";

/** A package by the name a person uses: its version, or its run while it has none. */
function shortName(pkg: CreativePackageSummary | null, fallback: string): string {
  if (!pkg) return fallback;
  return pkg.version > 0 ? `v${pkg.version}` : `unreleased, run ${middle(pkg.creative_run_id)}`;
}

function versionLabel(pkg: CreativePackageSummary): string {
  const name = pkg.version > 0 ? `v${pkg.version}` : `Unreleased (run ${middle(pkg.creative_run_id)})`;
  return `${name} · ${PACKAGE_STATUS[pkg.status].label.toLowerCase()}${pkg.released_at ? ` · ${shortDate(pkg.released_at)}` : ""}`;
}

/**
 * `/projects/[id]/creative/compare?a=&b=` — the package diff (Stage 04 PRD
 * §15.3, §15.4 L). `a` is the earlier package and `b` the later one; the
 * pickers rewrite the URL, so a comparison is a link that can be shared.
 */
export function CompareScreen({ projectId, before, after }: { projectId: string; before: string | null; after: string | null }) {
  const overview = useCreativeOverview(projectId);
  const diff = usePackageDiff(before, after);
  const router = useRouter();
  const packages = overview.data?.packages ?? [];
  const find = (id: string | null) => packages.find((p) => p.package_id === id) ?? null;

  const pick = (side: "a" | "b", id: string) => {
    const a = side === "a" ? id : (before ?? "");
    const b = side === "b" ? id : (after ?? "");
    router.replace(compareHref(projectId, a, b));
  };

  return (
    <div className="flex flex-col gap-8">
      <div className="flex flex-col gap-4">
        <Link
          href={`/projects/${projectId}/creative`}
          className="inline-flex w-fit items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
        >
          <ArrowLeft className="size-4 shrink-0" aria-hidden />
          Back to Copy &amp; creative
        </Link>
        <div>
          <h1 className="text-lg font-medium tracking-tight text-fg">Compare packages</h1>
          <p className="text-sm text-fg-muted">
            What the later package added, removed and changed against the earlier one, matched slot by slot.
          </p>
        </div>
        {overview.isPending ? (
          <Skeleton className="h-9 w-full max-w-xl" />
        ) : (
          <div className="flex flex-wrap items-end gap-3">
            {(["a", "b"] as const).map((side) => (
              <label key={side} className="flex min-w-56 flex-1 flex-col gap-1.5 text-sm sm:flex-none">
                <span className="font-medium text-fg">{side === "a" ? "Earlier" : "Later"}</span>
                <Select
                  value={(side === "a" ? before : after) ?? ""}
                  onChange={(event) => pick(side, event.target.value)}
                  data-testid={`compare-${side}`}
                >
                  <option value="">Choose a package</option>
                  {packages.map((p) => (
                    <option key={p.package_id} value={p.package_id}>
                      {versionLabel(p)}
                    </option>
                  ))}
                </Select>
              </label>
            ))}
          </div>
        )}
        {find(before) || find(after) ? (
          <p className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
            {(
              [
                ["earlier", find(before)],
                ["later", find(after)],
              ] as const
            ).map(([side, p]) =>
              p ? (
                <Link key={side} href={packageHref(projectId, p.package_id)} className="text-accent hover:underline">
                  Open the {side} package ({shortName(p, "")})
                </Link>
              ) : null,
            )}
          </p>
        ) : null}
      </div>

      {!before || !after ? (
        <EmptyState
          icon={GitCompareArrows}
          title="Choose two packages"
          description="Pick the earlier and the later package. Released versions and packages still waiting to be released can both be compared."
        />
      ) : before === after ? (
        <Alert tone="info" title="The same package twice">
          Choose a different package on one side to see what changed.
        </Alert>
      ) : diff.isPending ? (
        <Skeleton className="h-96 w-full" />
      ) : diff.error ? (
        <Alert tone="error" title="The packages could not be compared">
          {diff.error instanceof ApiError ? diff.error.message : "Refresh the page to try again."}
        </Alert>
      ) : (
        <PackageDiff
          diff={diff.data}
          beforeLabel={shortName(find(before), diff.data.against_version > 0 ? `v${diff.data.against_version}` : "the earlier package")}
          afterLabel={shortName(find(after), diff.data.version > 0 ? `v${diff.data.version}` : "the later package")}
        />
      )}
    </div>
  );
}
