/**
 * Root layout: the terminal frame around every page.
 *
 *   TerminalChrome  (sticky: brand bar + command line + clocks + chain chip + LED,
 *                    function keys F1 DESK · F2 FUND · F3 AGENTS · F4 SOURCES, ticker tape)
 *   <main>          the page
 *   footer          one muted line: testnet only, zero-gas agents, demo vendors
 *
 * Pages: `/` DESK dashboard, `/fund` (budget), `/agents`, `/sources` (data catalog + live x402
 * probe). `Providers` and the chrome are client components; this layout stays a server component.
 */

import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import { TerminalChrome } from "@/components/terminal/TerminalChrome";

import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "PocketDesk — research desk terminal",
  description:
    "Sector agents buy research per call over x402 in AUSD, within a capped, revocable budget. Testnet only.",
};

export const viewport: Viewport = {
  themeColor: "#000000",
  colorScheme: "dark",
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-bg text-fg">
        <Providers>
          <TerminalChrome />
          <main className="px-2 py-2">{children}</main>
          <footer className="px-2 pb-3 text-[11px] text-fg-muted">
            TESTNET ONLY · AGENTS HOLD 0 ETH — THE RELAYER PAYS ALL GAS · SELLERS ARE DEMO VENDORS (TESTNET STAND-INS)
          </footer>
        </Providers>
      </body>
    </html>
  );
}
