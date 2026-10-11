/**
 * TerminalChrome: the fixed top of every page — TopBar (brand, command line, clocks, chain chip,
 * live LED), FunctionTabs (F1 DESK … F4 SOURCES) and the TickerTape. Sticky, black, 3 rows.
 * Rendered once by `app/layout.tsx`; pages render below it.
 */

"use client";

import { FunctionTabs } from "./FunctionTabs";
import { TickerTape } from "./TickerTape";
import { TopBar } from "./TopBar";

export function TerminalChrome() {
  return (
    <header className="sticky top-0 z-40 bg-black">
      <TopBar />
      <FunctionTabs />
      <TickerTape />
    </header>
  );
}
