/**
 * Nav: the top bar on every page.
 *
 *   PocketDesk | Dashboard  Fund  Agents  Sources | [Base Sepolia · testnet] [wallet]
 *
 * The wallet button opens the Reown AppKit modal when a project id is configured, otherwise it
 * connects the browser's injected wallet (see `lib/appkit.ts`). When the connected wallet is on
 * another chain it offers a one-click switch to Base Sepolia, because every signature PocketDesk
 * asks for is bound to chain id 84532.
 */

"use client";

import { useAppKit } from "@reown/appkit/react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useAccount, useConnect, useDisconnect, useSwitchChain } from "wagmi";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { APPKIT_ENABLED, TARGET_CHAIN } from "@/lib/appkit";
import { shortAddress } from "@/lib/format";
import { cx } from "@/lib/ui";

const NAV_ITEMS = [
  { href: "/", label: "Dashboard" },
  { href: "/fund", label: "Fund" },
  { href: "/agents", label: "Agents" },
  { href: "/sources", label: "Sources" },
] as const;

/** Rule: "/" is active only on the dashboard itself; other items also match their sub-pages. */
function isActive(pathname: string, href: string): boolean {
  if (href === "/") {
    return pathname === "/";
  }
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function Nav() {
  const pathname = usePathname();
  return (
    <header className="sticky top-0 z-40 border-b border-line bg-bg/95 backdrop-blur">
      <div className="mx-auto flex h-11 max-w-[1600px] items-center gap-4 px-3">
        <Link href="/" className="num text-sm font-bold tracking-tight text-fg">
          Pocket<span className="text-up">Desk</span>
        </Link>
        <nav className="flex items-center gap-0.5">
          {NAV_ITEMS.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={cx(
                "rounded-sm px-2 py-1 text-xs font-medium transition-colors",
                isActive(pathname, item.href) ? "bg-surface-2 text-fg" : "text-fg-muted hover:text-fg",
              )}
            >
              {item.label}
            </Link>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-2">
          <Badge tone="warn" dot title="Every transaction runs on Base Sepolia with test tokens only">
            Base Sepolia · testnet
          </Badge>
          <WrongChainButton />
          {APPKIT_ENABLED ? <AppKitWalletButton /> : <InjectedWalletButton />}
        </div>
      </div>
    </header>
  );
}

/** Shown only when a wallet is connected on a chain other than Base Sepolia. */
function WrongChainButton() {
  const { isConnected, chainId } = useAccount();
  const { switchChain, isPending } = useSwitchChain();
  if (!isConnected || chainId === TARGET_CHAIN.id) {
    return null;
  }
  return (
    <Button size="sm" variant="danger" busy={isPending} onClick={() => switchChain({ chainId: TARGET_CHAIN.id })}>
      Switch to Base Sepolia
    </Button>
  );
}

/** AppKit mode: the modal handles connect, account details and disconnect. */
function AppKitWalletButton() {
  const { open } = useAppKit();
  const { address, isConnected } = useAccount();
  return (
    <Button size="sm" variant={isConnected ? "secondary" : "primary"} onClick={() => open()}>
      {isConnected ? <span className="num">{shortAddress(address)}</span> : "Connect wallet"}
    </Button>
  );
}

/** Injected-only mode: connect the browser wallet; clicking the address disconnects. */
function InjectedWalletButton() {
  const { address, isConnected } = useAccount();
  const { connect, connectors, isPending, error } = useConnect();
  const { disconnect } = useDisconnect();

  if (isConnected) {
    return (
      <Button size="sm" title="Disconnect" onClick={() => disconnect()}>
        <span className="num">{shortAddress(address)}</span>
      </Button>
    );
  }
  const injectedConnector = connectors[0];
  return (
    <Button
      size="sm"
      variant="primary"
      busy={isPending}
      disabled={injectedConnector === undefined}
      title={error ? error.message : "Connect a browser wallet (MetaMask, Rabby, …)"}
      onClick={() => connect({ connector: injectedConnector, chainId: TARGET_CHAIN.id })}
    >
      Connect wallet
    </Button>
  );
}
