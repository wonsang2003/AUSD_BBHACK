/**
 * CommandLine: the top-bar command input with a green <GO> button.
 *
 * Typing a function name jumps to it: DESK / F1, FUND / BUDGET / F2, AGENTS / F3, SOURCES / F4.
 * Anything else is a question for the desk: it is handed to the ASK THE DESK panel (via
 * `DeskCommand`) and the view switches to the DESK if needed. Press "/" anywhere to focus it.
 */

"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { useDeskCommand } from "./DeskCommand";
import { TermButton } from "./TermButton";

const COMMAND_ROUTES: Record<string, string> = {
  DESK: "/",
  F1: "/",
  HOME: "/",
  FUND: "/fund",
  BUDGET: "/fund",
  F2: "/fund",
  AGENTS: "/agents",
  AGENT: "/agents",
  F3: "/agents",
  SOURCES: "/sources",
  SOURCE: "/sources",
  CATALOG: "/sources",
  F4: "/sources",
};

/** A known function name → its route; anything else → null (a question). */
export function commandRoute(text: string): string | null {
  const key = text.trim().toUpperCase().replace(/\s*<?GO>?$/, "");
  return COMMAND_ROUTES[key] ?? null;
}

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) {
    return false;
  }
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

export function CommandLine() {
  const [text, setText] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const router = useRouter();
  const pathname = usePathname() ?? "/";
  const { askDesk } = useDeskCommand();

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent): void {
      if (event.key === "/" && !isTypingTarget(event.target)) {
        event.preventDefault();
        inputRef.current?.focus();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  function submit(event: FormEvent): void {
    event.preventDefault();
    const trimmed = text.trim();
    if (!trimmed) {
      return;
    }
    const route = commandRoute(trimmed);
    if (route) {
      router.push(route);
    } else {
      askDesk(trimmed);
      if (pathname !== "/") {
        router.push("/");
      }
    }
    setText("");
  }

  return (
    <form onSubmit={submit} className="flex min-w-0 flex-1 items-center gap-1">
      <span className="font-bold text-amber">&gt;</span>
      <input
        ref={inputRef}
        value={text}
        onChange={(event) => setText(event.target.value)}
        placeholder='FUND · AGENTS · SOURCES — or ask: "NVDA earnings risk?"   (press / to focus)'
        aria-label="Command line"
        className="t-input min-w-0 flex-1"
        spellCheck={false}
        autoComplete="off"
      />
      <TermButton type="submit" variant="go" size="md" disabled={!text.trim()}>
        &lt;GO&gt;
      </TermButton>
    </form>
  );
}
