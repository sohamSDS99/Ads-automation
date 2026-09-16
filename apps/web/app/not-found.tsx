import Link from "next/link";

export default function NotFound() {
  return (
    <div className="mx-auto flex max-w-md flex-col items-center gap-3 py-24 text-center">
      <p className="font-mono text-xs text-fg-subtle">404</p>
      <h1 className="text-[length:var(--text-lg)] font-semibold">This page does not exist yet</h1>
      <p className="text-sm text-fg-muted">
        Approvals, Evidence and Settings are built in Phases P6 and P7.
      </p>
      <Link href="/" className="text-sm text-accent underline underline-offset-4">
        Back to Projects
      </Link>
    </div>
  );
}
