"use client";

import { MailWarning } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { CopyButton } from "@/components/ui/copy-button";
import { shortDate } from "@/lib/format";
import { ROLE_LABEL } from "@/lib/permissions";
import type { InviteCreated } from "@/lib/api/users";

/**
 * The one moment an invite link exists in plaintext.
 *
 * Shared by the Team screen and the administrator's Accounts screen because
 * it is the same moment in both, and two copies of it would drift the way the
 * three invite endpoints behind them had already started to. The link is shown
 * whether or not the email went out: when SMTP is unconfigured, copying it is
 * the entire delivery mechanism (PRD §16).
 */
export function InviteResult({
  invite,
  showWorkspace = false,
}: {
  invite: InviteCreated;
  /** Name the workspace — needed where the admin chose it from a list. */
  showWorkspace?: boolean;
}) {
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
      ) : (
        <Alert tone="warning" title="No email was sent">
          <span className="flex gap-2">
            <MailWarning className="mt-0.5 size-4 shrink-0 text-status-gate" aria-hidden />
            SMTP is not configured on this deployment, so send this link to {invite.email}{" "}
            yourself. It is shown once.
          </span>
        </Alert>
      )}

      <div className="flex items-center gap-2 rounded-[var(--radius)] border bg-surface px-3 py-2.5">
        <code className="min-w-0 flex-1 truncate font-mono text-xs text-fg-muted">
          {invite.link}
        </code>
        <CopyButton value={invite.link} label="Copy link" />
      </div>

      <p className="text-xs text-fg-subtle">
        Expires {shortDate(invite.expires_at)} · role {ROLE_LABEL[invite.role]}
        {showWorkspace ? ` · ${invite.workspace_name}` : ""}
      </p>
    </>
  );
}
