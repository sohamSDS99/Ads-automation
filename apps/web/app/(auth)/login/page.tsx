import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { Suspense } from "react";

import { LoginForm } from "@/app/(auth)/login/login-form";
import { Wordmark } from "@/components/shell/wordmark";
import { getServerSession } from "@/lib/server-session";

export const metadata: Metadata = { title: "Sign in · Paid Ads Research Agent" };

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: string }>;
}) {
  // Already signed in: send them on rather than showing a form that would only
  // replace the session they already have.
  if (await getServerSession()) redirect("/");

  const { next } = await searchParams;
  return (
    <div className="flex flex-col gap-8">
      <Wordmark size="lg" />

      <div>
        <h1 className="text-[length:var(--text-xl)] font-semibold tracking-tight text-fg">
          Sign in
        </h1>
        <p className="mt-2 text-sm text-fg-muted">
          Research runs, evidence and reports for this workspace.
        </p>
      </div>

      <Suspense>
        <LoginForm next={safeNext(next)} />
      </Suspense>

      <p className="text-xs text-fg-muted">
        Accounts here are invite-only. If you cannot get in, ask a workspace admin to send you a
        new invite.
      </p>
    </div>
  );
}

/**
 * Only same-site paths are followed after sign-in.
 *
 * `?next=https://elsewhere.example` would otherwise turn the login page into an
 * open redirect, which is exactly the shape a phishing link wants.
 */
function safeNext(next: string | undefined): string {
  if (!next || !next.startsWith("/") || next.startsWith("//")) return "/";
  return next;
}
