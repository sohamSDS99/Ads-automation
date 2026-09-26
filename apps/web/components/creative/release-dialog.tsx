"use client";

import { Lock } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { PinsList, StopsTable } from "@/components/creative/package-sections";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogClose, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import { packageHref, type PackageView } from "@/lib/api/creative-packages";
import type { CreativePackageSummary } from "@/lib/api/creative";
import { confirmsVersion, formatBytes } from "@/lib/creative/package";
import { absoluteTime, relativeTime } from "@/lib/format";
import { useReleasePackage } from "@/lib/queries";

/**
 * `ReleaseDialog` — the one irreversible act in Stage 04 (PRD §15.4 L, law 42).
 *
 * It states what release does before it is done (§15.2 rule 8): the stops the
 * package records with who decided each and when, the pins it was written
 * against, the version it mints — the server's `version_to_mint`, never a
 * number worked out here — how many assets freeze and files are written, and
 * which version it supersedes. The approver types the version (`v3`) to
 * confirm, and the button says it: `Release v3`.
 *
 * Mounted only for a holder of `creative_release` on a package the server says
 * is releasable; for anyone else the control is absent, not disabled.
 */
export function ReleaseDialog({
  view,
  projectId,
  runId,
  released,
  open,
  onOpenChange,
}: {
  view: PackageView;
  projectId: string;
  runId: string;
  /** The project's currently released package, which this one would supersede. */
  released: CreativePackageSummary | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const router = useRouter();
  const release = useReleasePackage(projectId, runId);
  const [typed, setTyped] = useState("");
  const errorRef = useRef<HTMLDivElement>(null);
  // The body scrolls inside a height-capped dialog: bring a refusal into view,
  // as well as announcing it (the alert's role).
  useEffect(() => {
    if (release.error) errorRef.current?.scrollIntoView({ block: "nearest" });
  }, [release.error]);
  const version = view.release.version_to_mint;
  if (version === null) return null;

  const pkg = view.package;
  const texts = pkg.campaigns.reduce((sum, c) => sum + c.text_assets.length, 0);
  const media = pkg.campaigns.reduce((sum, c) => sum + c.media.length + c.logos.length, 0);
  const bytes = pkg.manifest.reduce((sum, entry) => sum + entry.bytes, 0);
  const confirmed = confirmsVersion(typed, version);
  const failure = release.error instanceof ApiError ? release.error : null;

  const submit = () => {
    if (!confirmed || release.isPending) return;
    release.mutate(
      { packageId: pkg.package_id, version },
      {
        onSuccess: (result) => {
          toast.success(`Released v${result.version}`, {
            description: `${result.files} ${result.files === 1 ? "file" : "files"} written and hashed. This is now the version launch reads.`,
          });
          onOpenChange(false);
          router.push(packageHref(projectId, result.package_id));
        },
      },
    );
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (release.isPending) return;
        if (!next) {
          setTyped("");
          release.reset();
        }
        onOpenChange(next);
      }}
    >
      <DialogContent
        title={`Release package v${version}`}
        description="Stage 05 loads the released version and nothing else. Check what it records, then type the version to confirm."
        className="flex max-h-main max-w-2xl flex-col"
        data-testid="release-dialog"
      >
        <form
          className="flex min-h-0 flex-1 flex-col"
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <DialogBody className="min-h-0 flex-1 overflow-y-auto">
            <div className="flex flex-col gap-2">
              <h3 className="text-sm font-medium text-fg">Stops</h3>
              <StopsTable stops={view.release.stops} compact />
            </div>
            <div className="flex flex-col gap-2">
              <h3 className="text-sm font-medium text-fg">Pins</h3>
              <PinsList pins={pkg.pins} />
            </div>
            <p className="text-sm text-fg tabular-nums" data-testid="release-consequence">
              Releasing mints <span className="font-medium">v{version}</span>, freezes the {texts} text{" "}
              {texts === 1 ? "asset" : "assets"}
              {media > 0 ? ` and ${media} media ${media === 1 ? "asset" : "assets"}` : ""} it ships, and writes{" "}
              {pkg.manifest.length} {pkg.manifest.length === 1 ? "file" : "files"} ({formatBytes(bytes)}) with their
              hashes.
              {released ? (
                <>
                  {" "}
                  v{released.version}, released{" "}
                  {released.released_at ? (
                    <time dateTime={released.released_at} title={absoluteTime(released.released_at)}>
                      {relativeTime(released.released_at)}
                    </time>
                  ) : null}
                  , becomes superseded.
                </>
              ) : null}
            </p>
            <Alert tone="warning" title="A released package is immutable">
              Once released, nothing in v{version} can be edited: its assets are frozen and its files hashed. A change
              needs a new creative run and a new version.
            </Alert>
            <Field
              label={`Type v${version} to confirm`}
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              autoComplete="off"
              spellCheck={false}
              placeholder={`v${version}`}
              data-testid="release-confirm"
              hint={`Exactly v${version}. The server checks it is still the version it will mint.`}
            />
            <div ref={errorRef}>
              {failure ? (
                <Alert tone="error" title={failure.problem?.title ?? "Release refused"}>
                  <span data-testid="release-error">{failure.message}</span> Nothing was released.
                </Alert>
              ) : release.error ? (
                <Alert tone="error" title="Release did not reach the server">
                  Check your connection, then try again. Nothing was released.
                </Alert>
              ) : null}
            </div>
          </DialogBody>
          <DialogFooter className="shrink-0">
            <DialogClose asChild>
              <Button type="button" variant="ghost" disabled={release.isPending}>
                Cancel
              </Button>
            </DialogClose>
            <Button type="submit" disabled={!confirmed || release.isPending} data-testid="release-submit">
              <Lock aria-hidden />
              {release.isPending ? `Releasing v${version} · writing ${pkg.manifest.length} files` : `Release v${version}`}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
