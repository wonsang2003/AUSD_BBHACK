/**
 * Root layout: the frame around every page (dark terminal theme, top Nav, footer note).
 *
 * Pages: `/` dashboard, `/fund` (budget: permit + caps), `/agents`, `/sources` (data catalog +
 * live x402 probe). `Providers` (wagmi + TanStack Query) is a client component; this layout
 * stays a server component.
 */

import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import { Nav } from "@/components/Nav";

import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "PocketDesk — a research desk on a retail budget",
  description:
    "Sector agents buy research per call over x402 in AUSD, within a capped, revocable budget. Base Sepolia testnet.",
};

export const viewport: Viewport = {
  themeColor: "#07090c",
  colorScheme: "dark",
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-bg text-fg">
        <Providers>
          <Nav />
          <main className="mx-auto max-w-[1600px] px-3 py-3">{children}</main>
          <footer className="mx-auto max-w-[1600px] px-3 pb-4 text-[11px] text-fg-dim">
            Testnet only. Agents hold 0 ETH: the relayer pays all gas. Our sellers are demo vendors (testnet stand-ins).
          </footer>
        </Providers>
      </body>
    </html>
  );
}
