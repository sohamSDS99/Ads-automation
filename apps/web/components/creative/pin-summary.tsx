import { ArrowUpRight } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

import { MonoId } from "@/components/creative/mono-id";
import type { CreativeEligibility } from "@/lib/api/creative";

function Pin({
  label,
  value,
  href,
  linkLabel,
}: {
  label: string;
  value: ReactNode;
  href: string;
  linkLabel: string;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-1 px-3 py-2.5">
      <dt className="text-xs text-fg-subtle">{label}</dt>
      <dd className="flex min-w-0 items-center gap-2 text-sm text-fg">
        {value}
        <Link
          href={href}
          aria-label={linkLabel}
          title={linkLabel}
          className="rounded-token p-0.5 text-fg-subtle transition-colors hover:text-fg"
        >
          <ArrowUpRight className="size-3.5" aria-hidden />
        </Link>
      </dd>
    </div>
  );
}

/**
 * `PinSummary` — the two frozen artifacts a run started now would write into,
 * and the context projected from them (PRD §4.4, law 32). Read-only: the pins
 * are the server's (`eligibility.pins`), taken at start, and each links to
 * the page that holds its source.
 */
export function PinSummary({
  projectId,
  pins,
}: {
  projectId: string;
  pins: CreativeEligibility["pins"];
}) {
  const planRun = typeof pins.plan_run_id === "string" ? pins.plan_run_id : null;
  const published = `/projects/${projectId}/guidelines/published`;
  return (
    <dl className="grid divide-y rounded-token border sm:grid-cols-3 sm:divide-x sm:divide-y-0">
      <Pin
        label="Plan"
        value={<span className="tabular-nums">v{pins.plan_version ?? "—"}</span>}
        href={planRun ? `/projects/${projectId}/plan/runs/${planRun}/plan` : `/projects/${projectId}/plan`}
        linkLabel={`Open plan v${pins.plan_version ?? ""}`}
      />
      <Pin
        label="Ruleset"
        value={<span className="font-mono text-xs">{pins.ruleset_version ?? "—"}</span>}
        href={published}
        linkLabel={`Open the published guidelines, ruleset ${pins.ruleset_version ?? ""}`}
      />
      <Pin
        label="Context"
        value={
          typeof pins.context_hash === "string" ? (
            <MonoId value={pins.context_hash} label="context hash" />
          ) : (
            <span className="text-fg-muted">—</span>
          )
        }
        href={published}
        linkLabel="Open the published guidelines this context is projected from"
      />
    </dl>
  );
}
