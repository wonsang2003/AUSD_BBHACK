/**
 * WalletPanel: step 1 on /fund — connect a wallet, see its AUSD balance, get test AUSD.
 *
 * Data: the connected account (wagmi), its AUSD balance read on-chain (`balanceOf` on Base
 * Sepolia through the browser RPC), and `POST /api/faucet`, where the relayer calls the AUSD
 * faucet for this address — the user never needs ETH.
 */

"use client";

import { useMutation } from "@tanstack/react-query";
import { erc20Abi, type Address } from "viem";
import { useAccount, useReadContract } from "wagmi";

import { errorText } from "@/components/dashboard/QueryState";
import { TxLink } from "@/components/dashboard/TxLink";
import { Button } from "@/components/ui/Button";
import { Stat } from "@/components/ui/Stat";
import { requestFaucet } from "@/lib/api";
import { TARGET_CHAIN } from "@/lib/appkit";
import { formatAusd, shortAddress } from "@/lib/format";

import { ConnectWalletButton } from "./ConnectWalletButton";

const BALANCE_REFRESH_MS = 15_000;

interface WalletPanelProps {
  ausdAddress: Address;
  explorerTxUrl?: string;
  onTx: (label: string, hash: string) => void;
}

export function WalletPanel({ ausdAddress, explorerTxUrl, onTx }: WalletPanelProps) {
  const { address, isConnected } = useAccount();
  const balance = useReadContract({
    address: ausdAddress,
    abi: erc20Abi,
    functionName: "balanceOf",
    args: address ? [address] : undefined,
    chainId: TARGET_CHAIN.id,
    query: { enabled: address !== undefined, refetchInterval: BALANCE_REFRESH_MS },
  });
  const faucet = useMutation({
    mutationFn: (owner: string) => requestFaucet(owner),
    onSuccess: ({ tx_hash }) => {
      onTx("Faucet: test AUSD to your wallet", tx_hash);
      setTimeout(() => void balance.refetch(), BALANCE_REFRESH_MS / 3);
    },
  });

  if (!isConnected || !address) {
    return (
      <div className="flex flex-col items-start gap-2 text-xs text-fg-muted">
        <p>Connect the wallet that will fund the budget. You only sign messages; you never pay gas.</p>
        <ConnectWalletButton />
      </div>
    );
  }
  return (
    <div className="flex flex-wrap items-end gap-6">
      <Stat label="Wallet" value={shortAddress(address)} sub="Base Sepolia" />
      <Stat
        label="AUSD balance"
        value={balance.data === undefined ? (balance.isError ? "n/a" : "…") : formatAusd(balance.data)}
        sub="read on-chain"
      />
      <div className="flex flex-col gap-1">
        <Button variant="primary" busy={faucet.isPending} onClick={() => faucet.mutate(address)}>
          Get test AUSD
        </Button>
        {faucet.data && <TxLink hash={faucet.data.tx_hash} explorerTxUrl={explorerTxUrl} />}
        {faucet.isError && <span className="text-[11px] text-down">{errorText(faucet.error)}</span>}
      </div>
    </div>
  );
}
