"""
AUSD testnet faucet: mint 10,000 test AUSD to a user so they can fund a budget.

Where this fits: users and agents never hold ETH, so they cannot call the faucet themselves.
The relayer calls `requestFunds(recipient)` for them and pays the gas. On a local anvil chain
the same code path works by pointing `faucet_address` at MockAUSD, which has the same function.
"""

from __future__ import annotations

from eth_utils import to_checksum_address

from app.chain.ausd import FAUCET_ABI
from app.chain.web3client import TxResult, TxSender


def request_test_ausd(sender: TxSender, faucet_address: str, recipient: str) -> TxResult:
    """Call `faucet.requestFunds(recipient)` from `sender` (the relayer). Raises TxRevertedError on revert."""
    faucet = sender.w3.eth.contract(address=to_checksum_address(faucet_address), abi=FAUCET_ABI)
    return sender.send(faucet.functions.requestFunds(to_checksum_address(recipient)))
