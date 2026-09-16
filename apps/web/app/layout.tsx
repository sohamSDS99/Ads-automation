import type { Metadata } from "next";
import type { ReactNode } from "react";

import { AppShell } from "@/components/shell/app-shell";
import { Providers } from "@/app/providers";

import "./globals.css";

export const metadata: Metadata = {
  title: "Paid Ads Research Agent",
  description: "Evidence-backed paid search research for SDS Manager.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  // `suppressHydrationWarning` is required: next-themes sets `data-theme` on
  // <html> before React hydrates, which is a deliberate mismatch.
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
