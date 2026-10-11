/**
 * FunctionTabs: the Bloomberg-style function-key row — "F1 DESK" "F2 FUND" "F3 AGENTS" "F4 SOURCES".
 *
 * Keyboard: Alt+1 … Alt+4 jump to a tab (real F-keys are left alone for the browser). The active
 * tab is solid amber with black text; the others are amber on black.
 */

"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";

import { cls } from "./tones";

export interface FunctionTab {
  key: string;
  label: string;
  href: string;
  hint: string;
}

export const FUNCTION_TABS: readonly FunctionTab[] = [
  { key: "F1", label: "DESK", href: "/", hint: "Live desk: budget, agents, purchases, findings (Alt+1)" },
  { key: "F2", label: "FUND", href: "/fund", hint: "Budget: open, caps, pause, revoke (Alt+2)" },
  { key: "F3", label: "AGENTS", href: "/agents", hint: "Sector agents and their settings (Alt+3)" },
  { key: "F4", label: "SOURCES", href: "/sources", hint: "Data catalog and live x402 probe (Alt+4)" },
];

/** Rule: "/" is active only on the desk itself; other tabs also match their sub-pages. */
export function isTabActive(pathname: string, href: string): boolean {
  if (href === "/") {
    return pathname === "/";
  }
  return pathname === href || pathname.startsWith(`${href}/`);
}

/** Alt+<digit> → tab index (0-based), or null. Uses `code` so it works on macOS (Option changes `key`). */
function tabIndexForKey(event: KeyboardEvent): number | null {
  if (!event.altKey || event.ctrlKey || event.metaKey) {
    return null;
  }
  const match = /^Digit([1-9])$/.exec(event.code);
  if (!match) {
    return null;
  }
  const index = Number(match[1]) - 1;
  return index < FUNCTION_TABS.length ? index : null;
}

export function FunctionTabs() {
  const pathname = usePathname() ?? "/";
  const router = useRouter();

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent): void {
      const index = tabIndexForKey(event);
      if (index === null) {
        return;
      }
      event.preventDefault();
      router.push(FUNCTION_TABS[index].href);
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [router]);

  return (
    <nav className="flex items-stretch gap-[2px] border-b border-line bg-black px-2 pt-1">
      {FUNCTION_TABS.map((tab) => {
        const active = isTabActive(pathname, tab.href);
        return (
          <Link
            key={tab.href}
            href={tab.href}
            title={tab.hint}
            className={cls(
              "flex items-center gap-1.5 border border-b-0 px-3 py-[3px] text-[12px] font-bold tracking-[0.04em]",
              active ? "border-amber bg-amber text-black" : "border-line bg-black text-amber hover:bg-[#1a1a1a]",
            )}
          >
            <span className={active ? "text-black/70" : "text-fg-muted"}>{tab.key}</span>
            <span>{tab.label}</span>
          </Link>
        );
      })}
      <span className="ml-auto self-center pb-[3px] text-[11px] text-fg-muted">ALT+1…4 TO SWITCH</span>
    </nav>
  );
}
