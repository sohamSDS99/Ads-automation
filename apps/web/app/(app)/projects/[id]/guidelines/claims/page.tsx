"use client";

import { useQuery } from "@tanstack/react-query";
import { useParams } from "next/navigation";

import { ClaimsRegister } from "@/components/claims/register";
import { Alert } from "@/components/ui/alert";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { ScrollText } from "lucide-react";
import { getSignOffMatrix, ownerLabel } from "@/lib/api/governance";
import { ApiError } from "@/lib/api";
import { errorMessage, keys, useClaims, useGuidelines } from "@/lib/queries";

/**
 * `/projects/[id]/guidelines/claims` — the register and the signature flow.
 *
 * The page resolves *which* guideline before the register can read anything.
 * Claims belong to a guideline, not to a project, and the one a person means
 * when they open this URL is the published version if there is one and the
 * live draft otherwise — the same precedence the rulebook viewer uses.
 */
export default function ClaimsPage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;

  const versions = useGuidelines(projectId);
  const matrix = useQuery({
    queryKey: keys.signoffMatrix(projectId),
    queryFn: () => getSignOffMatrix(projectId),
  });

  const list = versions.data?.versions ?? [];
  const current = list.find((item) => item.status === "published") ?? list[0];
  const claims = useClaims(current?.id ?? "");

  return (
    <div className="flex min-h-0 flex-col gap-6">
      <header>
        <h1 className="text-[length:var(--text-xl)] font-semibold tracking-tight">
          Claims register
        </h1>
        <p className="mt-1 max-w-prose text-sm text-fg-muted">
          Every factual assertion the copy makes, what substantiates it, and the named person
          who accepts responsibility for running it. Nothing here is licensed until they sign.
        </p>
      </header>

      {versions.isLoading ? (
        <div className="space-y-2" aria-busy>
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-64 w-full" />
        </div>
      ) : versions.isError ? (
        <Alert tone="error" title="This project's guidelines could not be read">
          {errorMessage(versions)}
        </Alert>
      ) : !current ? (
        <EmptyState
          icon={ScrollText}
          title="No guideline yet"
          description="Claims are registered by a guideline run. Start one from the Content Guidelines tab and its register will appear here."
        />
      ) : (
        <ClaimsRegister
          guidelineId={current.id}
          data={claims.data}
          loading={claims.isLoading}
          error={
            claims.isError
              ? claims.error instanceof ApiError
                ? claims.error.detail
                : errorMessage(claims)
              : null
          }
          onRefetch={() => {
            void claims.refetch();
            void matrix.refetch();
          }}
          ownerName={
            matrix.data?.matrix ? ownerLabel(matrix.data.matrix.legal_owner) : null
          }
        />
      )}
    </div>
  );
}
