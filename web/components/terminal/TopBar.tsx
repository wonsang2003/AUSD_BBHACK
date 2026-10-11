/**
 * TopBar: the terminal's first chrome row.
 *
 *   POCKETDESK  [ command line ……………………… <GO> ]   LCL 10:42:01  NY 13:42:01 [OPEN]   [LOCAL CHAIN 84532]  ● LIVE
 *
 * Command line: anything typed is a question for the desk — it is handed to the ASK THE DESK
 * panel (`DeskCommand`) and the app switches to the DESK tab. Typing a function code instead
 * ("DESK", "FUND", "AGENTS", "SOURCES" or "F1"…"F4") jumps to that tab.
 * Chain chip: from `useChainInfo()`. LED: green when the SSE live feed is connected.
 */

"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { Clock } from "./Clock";
import { useDeskCommand } from "./DeskCommand";
import { FUNCTION_TABS } from "./FunctionTabs";
import { useStreamStatus } from "./StreamProvider";
import { TermButton } from "./TermButton";
import { cls } from "./tones";
import { useChainInfo } from "./useChainInfo";

/** "FUND" / "F2" → "/fund"; anything else → null. */
function tabHrefForCommand(text: string): string | null {
  const command = text.trim().toUpperCase();
  const tab = FUNCTION_TABS.find((item) => item.label === command || item.key === command);
  return tab ? tab.href : null;
}

function ChainChip() {
  const chain = useChainInfo();
  if (chain.unreachable) {
    return (
      <span className="border border-down px-2 py-[1px] text-[11px] font-bold text-down" title="GET /api/health failed">
        BACKEND OFFLINE
      </span>
    );
  }
  return (
    <span
      className={cls(
        "border px-2 py-[1px] text-[11px] font-bold tracking-[0.04em]",
        chain.isLocal ? "border-info text-info" : "border-warn text-warn",
      )}
      title={chain.isLocal ? "Local anvil chain: test tokens, no explorer" : "Base Sepolia testnet: test tokens only"}
    >
      {chain.label}
    </span>
  );
}

function ConnectionLed() {
  const { status } = useStreamStatus();
  const connected = status === "open";
  return (
    <span className="flex items-center gap-1.5 text-[11px] font-bold" title={`Live feed (SSE): ${status}`}>
      <span className={cls("inline-block h-[9px] w-[9px] rounded-full", connected ? "bg-up" : "t-blink bg-down")} />
      <span className={connected ? "text-up" : "text-down"}>{connected ? "LIVE" : status.toUpperCase()}</span>
    </span>
  );
}

function CommandLine() {
  const [text, setText] = useState("");
  const { askDesk } = useDeskCommand();
  const router = useRouter();
  const pathname = usePathname() ?? "/";

  function submit(event: FormEvent): void {
    event.preventDefault();
    const trimmed = text.trim();
    if (!trimmed) {
      return;
    }
    const href = tabHrefForCommand(trimmed);
    if (href) {
      router.push(href);
    } else {
      askDesk(trimmed);
      if (pathname !== "/") {
        router.push("/");
      }
    }
    setText("");
  }

  return (
    <form onSubmit={submit} className="flex min-w-0 flex-1 items-center gap-0" role="search">
      <span className="flex h-[26px] items-center border border-r-0 border-line bg-black px-2 font-bold text-amber">&gt;</span>
      <input
        value={text}
        onChange={(event) => setText(event.target.value)}
        placeholder="Ask the desk (e.g. NVDA implied move into earnings?) or type FUND / AGENTS / SOURCES"
        aria-label="Command line"
        className="t-input flex-1"
      />
      <TermButton type="submit" variant="go" disabled={!text.trim()} title="Run (Enter)">
        &lt;GO&gt;
      </TermButton>
    </form>
  );
}

export function TopBar() {
  return (
    <div className="flex h-[36px] items-center gap-4 border-b border-line bg-black px-3">
      <Link href="/" className="shrink-0 text-[15px] font-bold tracking-[0.08em] text-amber">
        POCKETDESK
      </Link>
      <CommandLine />
      <Clock />
      <ChainChip />
      <ConnectionLed />
    </div>
  );
}
