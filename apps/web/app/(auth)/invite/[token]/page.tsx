import { Clock, CircleCheck, Link2Off } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import { AcceptInviteForm } from "@/app/(auth)/invite/[token]/accept-form";
import { Wordmark } from "@/components/shell/wordmark";
import { buttonVariants } from "@/components/ui/button-variants";
import { RoleBadge } from "@/components/ui/role-badge";
import { ROLE_DESCRIPTION } from "@/lib/permissions";
import type { InvitePreview } from "@/lib/api";
import { cn } from "@/lib/utils";

export const metadata: Metadata = { title: "Accept your invite · Paid Ads Research Agent" };

const API_INTERNAL_URL = process.env.API_INTERNAL_URL ?? "http://api:8000";

/**
 * The token is checked on the server before the form is ever drawn.
 *
 * A dead link should say what is wrong with it and what to do next, not hand
 * over a password field that will fail on submit.
 */
async function preview(token: string): Promise<InvitePreview> {
  try {
    const response = await fetch(
      `${API_INTERNAL_URL}/api/v1/invites/${encodeURIComponent(token)}`,
      { cache: "no-store", headers: { accept: "application/json" } },
    );
    if (!response.ok) return unusable();
    return (await response.json()) as InvitePreview;
  } catch {
    return unusable();
  }
}

function unusable(): InvitePreview {
  return {
    state: "invalid",
    email: null,
    role: null,
    workspace_name: null,
    expires_at: null,
    has_account: false,
  };
}

export default async function InvitePage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  const invite = await preview(token);

  if (invite.state !== "valid" || !invite.role) {
    return <DeadLink state={invite.state} />;
  }

  return (
    <div className="flex flex-col gap-8">
      <Wordmark size="lg" />

      <div>
        <h1 className="text-[length:var(--text-xl)] font-semibold tracking-tight text-fg">
          Join {invite.workspace_name ?? "the workspace"}
        </h1>
        {/* Two different sentences because they are two different acts. A new
            person is creating an account; someone who already has one is
            adding a workspace to it, and telling them to "choose a password"
            would read as an instruction to change the one they use. */}
        <p className="mt-2 text-sm text-fg-muted">
          {invite.has_account
            ? "You already have an account. Confirm your password to add this workspace to it — everything you have elsewhere stays exactly as it is."
            : "Choose a password and your account is ready. This link works once."}
        </p>
      </div>

      <dl className="flex flex-col gap-3 rounded-[var(--radius)] border bg-surface px-3.5 py-3 text-sm">
        <div className="flex items-baseline justify-between gap-4">
          <dt className="text-fg-muted">Email</dt>
          <dd className="min-w-0 truncate font-medium text-fg">{invite.email}</dd>
        </div>
        <div className="flex items-baseline justify-between gap-4">
          <dt className="text-fg-muted">Role</dt>
          <dd className="flex min-w-0 flex-col items-end gap-1">
            <RoleBadge role={invite.role} />
            <span className="text-xs text-fg-subtle">{ROLE_DESCRIPTION[invite.role]}</span>
          </dd>
        </div>
      </dl>

      <AcceptInviteForm
        token={token}
        email={invite.email ?? ""}
        hasAccount={invite.has_account}
        workspaceName={invite.workspace_name ?? "this workspace"}
      />
    </div>
  );
}

/** The three ways a link can be dead, each with its own answer. */
function DeadLink({ state }: { state: InvitePreview["state"] }) {
  const screens: Record<string, { icon: ReactNode; title: string; body: string }> = {
    expired: {
      icon: <Clock className="size-5 text-status-gate" aria-hidden />,
      title: "This invite has expired",
      body: "Invites are good for seven days. Ask a workspace admin to send you a new one.",
    },
    accepted: {
      icon: <CircleCheck className="size-5 text-status-success" aria-hidden />,
      title: "This invite has already been used",
      body: "Your account exists — sign in with the password you chose.",
    },
    invalid: {
      icon: <Link2Off className="size-5 text-fg-subtle" aria-hidden />,
      title: "This link isn't valid",
      body: "It may have been mistyped or cut short by an email client. Ask for a fresh invite.",
    },
  };
  const screen = screens[state] ?? screens.invalid!;

  return (
    <div className="flex flex-col gap-6">
      <Wordmark size="lg" />
      <div className="flex flex-col gap-3">
        {screen.icon}
        <h1 className="text-[length:var(--text-lg)] font-semibold tracking-tight text-fg">
          {screen.title}
        </h1>
        <p className="text-sm text-fg-muted">{screen.body}</p>
      </div>
      <Link href="/login" className={cn(buttonVariants({ variant: "secondary" }), "h-10 w-full")}>
        Go to sign in
      </Link>
    </div>
  );
}
