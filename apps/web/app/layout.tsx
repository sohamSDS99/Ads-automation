import type { Metadata } from "next";
import type { ReactNode } from "react";

import { Providers } from "@/app/providers";

import "./globals.css";

export const metadata: Metadata = {
  title: "Paid Ads Research Agent",
  description: "Evidence-backed paid search research for SDS Manager.",
};

/**
 * The root layout deliberately renders no shell.
 *
 * `/login` and `/invite/[token]` are reached without a session and must not
 * show navigation to a workspace the visitor may not be in. The shell belongs
 * to the `(app)` group, which cannot render at all without a session.
 */
export default function RootLayout({ children }: { children: ReactNode }) {
  // `suppressHydrationWarning` is required: next-themes sets `data-theme` on
  // <html> before React hydrates, which is a deliberate mismatch.
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
