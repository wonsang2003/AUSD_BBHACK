"""
Load compiled contract artifacts (ABI + bytecode) exported from Foundry.

`contracts/script/export_abi.sh` writes `backend/app/chain/abi/<Name>.json` as
{"abi": [...], "bytecode": "0x..."}; the files are committed so the backend and its
tests only need an RPC node (or anvil), not a Solidity toolchain.
"""

import json
from functools import lru_cache
from pathlib import Path

ABI_DIR = Path(__file__).parent / "abi"


@lru_cache
def load_artifact(name: str) -> tuple[list, str]:
    """Return (abi, bytecode) for a contract name such as "BudgetController" or "MockAUSD"."""
    data = json.loads((ABI_DIR / f"{name}.json").read_text())
    return data["abi"], data["bytecode"]
