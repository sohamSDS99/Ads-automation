"use client";

/**
 * What is on the Volume, and for how long (PRD §13.4 E, §16 "Volume full").
 *
 * The bar is the point, not decoration: a Volume is a fixed size and the only
 * question an admin has is "how close am I". It is drawn from real numbers the
 * worker measured, and when the worker cannot be reached the card says so
 * rather than drawing an empty bar — a zero here reads as "plenty of room",
 * which is the one wrong answer that costs a run.
 */

import { Alert } from "@/components/ui/alert";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessage, useStorageUsage } from "@/lib/queries";
import { cn } from "@/lib/utils";

const RETENTION_LABELS: Record<string, string> = {
  screenshots: "Competitor screenshots",
  debug: "Connector failure dumps",
  exports: "Rendered exports",
  backups: "Database backups",
};

export function StorageCard() {
  const usage = useStorageUsage();
  const error = errorMessage(usage);
  const data = usage.data;

  return (
    <Card>
      <CardHeader
        title="Storage"
        description="Everything that has to survive a deploy — creative screenshots, rendered exports, nightly database backups — lives on the worker's volume."
      />
      <CardBody className="space-y-4">
        {error ? (
          <Alert tone="error" title="Storage usage could not be read">
            {error}
          </Alert>
        ) : null}
        {usage.isPending ? <Skeleton className="h-20 w-full" /> : null}

        {data && !data.reachable ? (
          <Alert tone="warning" title="The worker is not reachable">
            Usage cannot be measured from here — the volume is attached to the worker, and it is
            not answering. Everything else on this page is unaffected.
          </Alert>
        ) : null}

        {data?.reachable ? <UsageMeter usage={data} /> : null}

        {data ? (
          <dl className="grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
            {Object.entries(data.retention).map(([key, days]) => (
              <div key={key} className="flex items-baseline justify-between gap-4 border-b py-1.5">
                <dt className="text-fg-muted">{RETENTION_LABELS[key] ?? key}</dt>
                <dd className="tabular-nums text-fg">
                  {days > 0 ? `kept ${days} days` : "kept forever"}
                </dd>
              </div>
            ))}
          </dl>
        ) : null}

        {data ? (
          <p className="text-xs text-fg-subtle">
            Pruned nightly. A screenshot that ages out leaves its evidence record intact — the
            finding and its citation stay, the picture does not.
          </p>
        ) : null}
      </CardBody>
    </Card>
  );
}

function UsageMeter({
  usage,
}: {
  usage: { bytes: number; objects: number; capacity_bytes: number | null; used_fraction: number | null; at_capacity: boolean; warn_above: number };
}) {
  const fraction = usage.used_fraction;
  const percent = fraction === null ? null : Math.min(Math.max(fraction, 0), 1) * 100;

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <p className="text-[length:var(--text-md)] tabular-nums text-fg">
          {formatBytes(usage.bytes)}
          {usage.capacity_bytes ? (
            <span className="text-fg-muted"> of {formatBytes(usage.capacity_bytes)}</span>
          ) : null}
        </p>
        <p className="text-sm tabular-nums text-fg-muted">
          {usage.objects.toLocaleString()} {usage.objects === 1 ? "file" : "files"}
        </p>
      </div>

      {percent === null ? null : (
        <div
          role="meter"
          aria-valuenow={Math.round(percent)}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label="Volume used"
          className="h-2 w-full overflow-hidden rounded-full bg-surface-hover"
        >
          <div
            className={cn(
              "h-full rounded-full transition-[width] duration-500 ease-out",
              usage.at_capacity ? "bg-status-failed" : "bg-accent",
            )}
            style={{ width: `${Math.max(percent, 1)}%` }}
          />
        </div>
      )}

      {usage.at_capacity ? (
        <Alert tone="error" title="The volume is filling up">
          Past {Math.round(usage.warn_above * 100)}% used. Runs write screenshots and exports as
          they go, so a full volume fails a run part-way. Shorten a retention window below, or give
          the worker a larger volume.
        </Alert>
      ) : null}
    </div>
  );
}

/** Binary units, because that is what a volume is provisioned in. */
function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1024;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(size >= 100 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}
