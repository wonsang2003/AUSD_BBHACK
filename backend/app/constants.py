"""
Fixed protocol constants for PocketDesk.

Everything in here is a *fact about the outside world* (token decimals, signing
domains, chain ids, typed-data type strings). Values that a user or operator may
want to change live in `app/config.py` instead.
"""

# ─── Network: Base Sepolia ────────────────────────────────────────────────────
BASE_SEPOLIA_CHAIN_ID = 84532
BASE_SEPOLIA_CAIP2 = "eip155:84532"  # CAIP-2 network id used inside x402 payloads
BASESCAN_TX_URL = "https://sepolia.basescan.org/tx/"
BASESCAN_ADDRESS_URL = "https://sepolia.basescan.org/address/"

# ─── AUSD token (Agora Dollar) ────────────────────────────────────────────────
AUSD_TESTNET_ADDRESS = "0xa9012a055bd4e0eDfF8Ce09f960291C09D5322dC"
AUSD_FAUCET_ADDRESS = "0xd236c18D274E54FAccC3dd9DDA4b27965a73ee6C"
AUSD_MAINNET_ADDRESS = "0x00000000eFE302BEAA2b3e6e1b18d08D69a9012a"  # reference only

AUSD_DECIMALS = 6
ONE_AUSD = 1_000_000  # 1 AUSD in atomic units (10 ** AUSD_DECIMALS)

# IMPORTANT: `name()` on the token returns "AUSD", but the EIP-712 *signing*
# domain name is "Agora Dollar". Signing with "AUSD" fails silently.
AUSD_EIP712_NAME = "Agora Dollar"
AUSD_EIP712_VERSION = "1"

# ─── PocketDesk BudgetController signing domain ──────────────────────────────
POCKETDESK_EIP712_NAME = "PocketDesk"
POCKETDESK_EIP712_VERSION = "1"

# ─── x402 protocol ───────────────────────────────────────────────────────────
X402_VERSION = 2
HEADER_PAYMENT_REQUIRED = "PAYMENT-REQUIRED"    # server → client, on HTTP 402
HEADER_PAYMENT_SIGNATURE = "PAYMENT-SIGNATURE"  # client → server, the signed payment
HEADER_PAYMENT_RESPONSE = "PAYMENT-RESPONSE"    # server → client, settlement receipt
LEGACY_HEADER_PAYMENT = "X-PAYMENT"             # x402 v1 (accepted by our server only)
LEGACY_HEADER_PAYMENT_RESPONSE = "X-PAYMENT-RESPONSE"

# validAfter is backdated a little so small clock skew between us and the chain
# does not make a fresh authorization "not yet valid".
ERC3009_VALID_AFTER_SKEW_SECONDS = 30
DEFAULT_PAYMENT_TIMEOUT_SECONDS = 120

# ─── Sectors (stored on-chain as bytes32 labels) ─────────────────────────────
AGENT_NAMES = ("OnchainPerps", "OptionsEvent", "NewsSentiment", "MacroFilings")
