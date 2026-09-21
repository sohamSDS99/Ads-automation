"use client";

import { Link2, MailWarning } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { CopyButton } from "@/components/ui/copy-button";
import { shortDate } from "@/lib/format";
import { ROLE_LABEL } from "@/lib/permissions";
import { useWorkspace } from "@/lib/queries";
import type { InviteCreated } from "@/lib/api/users";

/**
 * The one moment an invite link exists in plaintext.
 *
 * Shared by the Team screen and the administrator's Accounts screen because
 * it is the same moment in both, and two copies of it would drift the way the
 * three invite endpoints behind them had already started to.
 *
 * Three states, said differently on purpose. With no mail server configured,
 * handing the link over *is* the process and the panel says so plainly — this
 * is an internal tool, and framing the normal path as "no email was sent"
 * trained people to read a warning on every single invite. A configured
 * server that then failed is a real problem and still gets the warning.
 */
export function InviteResult({
  invite,
  showWorkspace = false,
}: {
  invite: InviteCreated;
  /** Name the workspace — needed where the admin chose it from a list. */
  showWorkspace?: boolean;
}) {
  // Whether a mail server exists at all is what separates "we did not email
  // them" from "we tried and failed", and only one of those is a warning.
  const mailConfigured = useWorkspace().data?.smtp_configured ?? false;

  return (
    <>
      {invite.has_account ? (
        <Alert tone="info" title="They already have an account">
          {invite.email} already works in another workspace on this installation. The link adds{" "}
          {showWorkspace ? invite.workspace_name : "this workspace"} to the account they already
          have — they confirm with their existing password, and nothing about that account
          changes.
        </Alert>
      ) : null}

      {invite.email_delivered ? (
        <p className="text-sm text-fg">
          An email is on its way to <strong>{invite.email}</strong>. The link below is the same
          one, in case you would rather send it yourself.
        </p>
      ) : mailConfigured ? (
        <Alert tone="warning" title="The email could not be sent">
          <span className="flex gap-2">
            <MailWarning className="mt-0.5 size-4 shrink-0 text-status-gate" aria-hidden />
            A mail server is configured but the send failed, so pass this link to {invite.email}{" "}
            yourself.
          </span>
        </Alert>
      ) : (
        <p className="flex gap-2 text-sm text-fg">
          <Link2 className="mt-0.5 size-4 shrink-0 text-fg-subtle" aria-hidden />
          <span>
            Send this link to <strong>{invite.email}</strong> however you like — it is how they
            join. Opening it lets them set their own password; nobody else ever sets it.
          </span>
        </p>
      )}

      <div className="flex items-center gap-2 rounded-[var(--radius)] border bg-surface px-3 py-2.5">
        <code className="min-w-0 flex-1 truncate font-mono text-xs text-fg-muted">
          {invite.link}
        </code>
        <CopyButton value={invite.link} label="Copy link" />
      </div>

      <p className="text-xs text-fg-subtle">
        Expires {shortDate(invite.expires_at)} · role {ROLE_LABEL[invite.role]}
        {showWorkspace ? ` · ${invite.workspace_name}` : ""} · single use
      </p>
    </>
  );
}
