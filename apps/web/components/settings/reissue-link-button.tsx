"use client";

import { useMutation } from "@tanstack/react-query";
import { Link2 } from "lucide-react";
import { useState } from "react";

import { InviteResult } from "@/components/settings/invite-result";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { Tooltip } from "@/components/ui/tooltip";
import { ApiError } from "@/lib/api";
import type { InviteCreated } from "@/lib/api/users";

/**
 * "Get link" for somebody who has not accepted yet.
 *
 * The link was shown once when the invite was made and cannot be shown again:
 * the token lives in the database only as a SHA-256, so there is nothing to
 * look up. Pressing this mints a *new* one and kills the old, which is the
 * only honest recovery and is said on the button's tooltip rather than
 * discovered afterwards.
 *
 * It matters more here than it would elsewhere. With no mail server, handing
 * the link over is the entire onboarding process, so an admin who closed the
 * dialog too early had stranded that person with no way forward.
 */
export function ReissueLinkButton({
  name,
  reissue,
  showWorkspace = false,
  disabled = false,
}: {
  /** Whose link it is, for the dialog title and the accessible name. */
  name: string;
  reissue: () => Promise<InviteCreated>;
  showWorkspace?: boolean;
  disabled?: boolean;
}) {
  const [created, setCreated] = useState<InviteCreated | null>(null);

  const mint = useMutation({
    mutationFn: reissue,
    onSuccess: setCreated,
    onError: (error) =>
      toast.error("No new link", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      }),
  });

  return (
    <>
      <Tooltip content="Creates a new single-use link. Any link sent earlier stops working.">
        <Button
          size="sm"
          variant="secondary"
          disabled={disabled || mint.isPending}
          aria-label={`Get an invite link for ${name}`}
          onClick={() => mint.mutate()}
        >
          {mint.isPending ? <Spinner label="Creating a link" /> : <Link2 aria-hidden />}
          Get link
        </Button>
      </Tooltip>

      <Dialog open={created !== null} onOpenChange={(next) => (next ? null : setCreated(null))}>
        <DialogContent title={`Invite link for ${name}`}>
          {created ? (
            <>
              <DialogBody>
                <InviteResult invite={created} showWorkspace={showWorkspace} />
              </DialogBody>
              <DialogFooter>
                <Button onClick={() => setCreated(null)}>Done</Button>
              </DialogFooter>
            </>
          ) : null}
        </DialogContent>
      </Dialog>
    </>
  );
}
