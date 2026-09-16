import type { ReactNode } from "react";

/**
 * The frame for the two pages reachable without a session.
 *
 * One column, one card, nothing else on screen. There is exactly one thing to
 * do here, and no navigation that would be honest to show to someone who is not
 * signed in yet.
 */
export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-dvh flex-col bg-bg">
      <main className="flex flex-1 items-center justify-center px-6 py-12">
        <div className="w-full max-w-sm">{children}</div>
      </main>
      <footer className="px-6 pb-8 text-center text-xs text-fg-muted">
        Paid Ads Research Agent · Stage 01
      </footer>
    </div>
  );
}
