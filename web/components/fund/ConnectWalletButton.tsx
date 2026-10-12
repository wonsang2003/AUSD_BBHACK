/**
 * ConnectWalletButton: the big "Connect wallet" call to action on /fund.
 *
 * Opens the Reown AppKit modal when a project id is configured (`APPKIT_ENABLED`), otherwise
 * connects the browser's injected wallet (MetaMask, Rabby, …) on Base Sepolia. Same rules as
 * the Nav's wallet button (`components/Nav.tsx`).
 */

"use client";

import { useAppKit } from "@reown/appkit/react";
import { useConnect } from "wagmi";

import { Button } from "@/components/ui/Button";
import { APPKIT_ENABLED, TARGET_CHAIN } from "@/lib/appkit";

export function ConnectWalletButton() {
  return APPKIT_ENABLED ? <AppKitConnect /> : <InjectedConnect />;
}

function AppKitConnect() {
  const { open } = useAppKit();
  return (
    <Button variant="primary" onClick={() => open()}>
      Connect wallet
    </Button>
  );
}

function InjectedConnect() {
  const { connect, connectors, isPending, error } = useConnect();
  const injectedConnector = connectors[0];
  return (
    <div className="flex flex-col gap-1">
      <Button
        variant="primary"
        busy={isPending}
        disabled={injectedConnector === undefined}
        onClick={() => connect({ connector: injectedConnector, chainId: TARGET_CHAIN.id })}
      >
        Connect browser wallet
      </Button>
      {error && <span className="text-[11px] text-down">{error.message}</span>}
    </div>
  );
}
