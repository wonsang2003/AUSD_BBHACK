#!/usr/bin/env bash
# export_abi.sh — build the contracts and export ABI + bytecode for the Python backend.
#
# Where this fits: the backend (web3.py) and its tests deploy and call BudgetController and
# MockAUSD from committed JSON artifacts, so they only need a local anvil node, not forge.
# Run this after every contract change and commit the outputs:
#
#   backend/app/chain/abi/BudgetController.json   {"abi": [...], "bytecode": "0x..."}
#   backend/app/chain/abi/MockAUSD.json           {"abi": [...], "bytecode": "0x..."}
#
# Usage:   contracts/script/export_abi.sh
# Offline: FOUNDRY_SOLC=/path/to/solc-0.8.24 contracts/script/export_abi.sh
# Uses jq when available, otherwise python3.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTRACTS_DIR="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(dirname "$CONTRACTS_DIR")"
ABI_OUT_DIR="$REPO_ROOT/backend/app/chain/abi"
CONTRACT_NAMES=("BudgetController" "MockAUSD")

# Foundry installs to ~/.foundry/bin; add it in case this shell has not.
export PATH="$HOME/.foundry/bin:$PATH"

# Write {"abi": ..., "bytecode": ...} from a forge artifact ($1) to an output file ($2).
extract_abi_and_bytecode() {
  local artifact="$1"
  local output="$2"
  if command -v jq >/dev/null 2>&1; then
    jq '{abi: .abi, bytecode: .bytecode.object}' "$artifact" > "$output"
  else
    python3 - "$artifact" "$output" <<'PY'
import json
import sys

artifact_path, output_path = sys.argv[1], sys.argv[2]
with open(artifact_path) as artifact_file:
    artifact = json.load(artifact_file)
exported = {"abi": artifact["abi"], "bytecode": artifact["bytecode"]["object"]}
with open(output_path, "w") as output_file:
    json.dump(exported, output_file, indent=2)
    output_file.write("\n")
PY
  fi
}

cd "$CONTRACTS_DIR"
forge build

mkdir -p "$ABI_OUT_DIR"
for name in "${CONTRACT_NAMES[@]}"; do
  artifact="$CONTRACTS_DIR/out/$name.sol/$name.json"
  if [[ ! -f "$artifact" ]]; then
    echo "error: forge artifact not found: $artifact" >&2
    exit 1
  fi
  extract_abi_and_bytecode "$artifact" "$ABI_OUT_DIR/$name.json"
  echo "wrote $ABI_OUT_DIR/$name.json"
done
