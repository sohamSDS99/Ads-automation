"use client";

import { Globe, ScanSearch } from "lucide-react";

import { CredentialCard } from "@/components/setup/credential-card";
import { CsvMapper } from "@/components/setup/csv-mapper";
import { Alert } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { credentialFor, type CredentialKindInfo } from "@/lib/api/credentials";
import { errorMessage, useCredentials } from "@/lib/queries";

/**
 * Step 2 — where the evidence comes from.
 *
 * Five sources, and they are not five of the same thing: three need a key, one
 * is a browser crawl of a public site, and one is a file you upload. Giving
 * each the same card would say they need the same attention, and they do not —
 * so the ones that can be misconfigured get the space, and the ones that cannot
 * are stated and left alone.
 *
 * The keyed list is not spelled out here. It comes from `GET /credentials`,
 * which reports every kind this build knows about, so adding a source to the
 * backend adds its card without a change on this screen.
 */
export function StepSources({
  projectId,
  canWriteCredentials,
  canUpload,
}: {
  projectId: string;
  canWriteCredentials: boolean;
  canUpload: boolean;
}) {
  const credentials = useCredentials();
  const error = errorMessage(credentials);

  const keyed = (credentials.data?.kinds ?? []).filter((kind) => kind.where === "sources");

  return (
    <div className="space-y-6">
      {error ? (
        <Alert tone="error" title="Connected sources could not be loaded">
          {error}
        </Alert>
      ) : null}

      <section className="space-y-3">
        <div>
          <h3 className="text-sm font-medium text-fg">Sources that need a key</h3>
          <p className="text-sm text-fg-muted">
            Every one is optional. Without them the run continues and the report names the
            sections that went without evidence — it never fills the gap with a guess.
          </p>
        </div>

        {credentials.isPending ? (
          <>
            <Skeleton className="h-32 w-full" />
            <Skeleton className="h-32 w-full" />
          </>
        ) : null}

        {keyed.map((spec: CredentialKindInfo) => (
          <CredentialCard
            key={spec.kind}
            spec={spec}
            credential={credentialFor(credentials.data?.credentials ?? [], spec.kind)}
            canWrite={canWriteCredentials}
            reason="Only an admin can store API keys. Ask one to connect this source — you can finish the rest of the setup now."
          />
        ))}
      </section>

      <section className="space-y-3">
        <h3 className="text-sm font-medium text-fg">Sources that need nothing</h3>
        <ul className="divide-y rounded-[var(--radius)] border bg-surface-raised">
          <SourceNote
            icon={ScanSearch}
            title="Google Ads Transparency Center"
            description="Competitor and own live ads, read from the public archive with a browser. No credential, no login-walled content."
          />
          <SourceNote
            icon={Globe}
            title="Your own site"
            description="Landing pages, CTAs, forms and page-speed signals, crawled from the site URL on the previous step."
          />
        </ul>
      </section>

      <section className="space-y-3">
        <div>
          <h3 className="text-sm font-medium text-fg">CRM export</h3>
          <p className="text-sm text-fg-muted">
            Closed-won and closed-lost deals. This is how the run learns which customers were worth
            winning, which no ad platform can tell it.
          </p>
        </div>
        {canUpload ? (
          <CsvMapper projectId={projectId} disabled={false} />
        ) : (
          <p className="rounded-[var(--radius)] border border-dashed px-3 py-4 text-sm text-fg-muted">
            Your role cannot upload files to this project.
          </p>
        )}
      </section>
    </div>
  );
}

function SourceNote({
  icon: Icon,
  title,
  description,
}: {
  icon: typeof Globe;
  title: string;
  description: string;
}) {
  return (
    <li className="flex gap-3 px-4 py-3.5">
      <Icon className="mt-0.5 size-4 shrink-0 text-fg-subtle" aria-hidden />
      <div>
        <p className="font-medium text-fg">{title}</p>
        <p className="max-w-prose text-sm text-fg-muted">{description}</p>
      </div>
    </li>
  );
}
