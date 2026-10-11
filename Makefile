# PocketDesk — common commands. Run from the repo root.
# On-chain targets need the repo-root .env (see .env.example) and network access to Base Sepolia.

install:            ## install backend + web dependencies
	cd backend && uv sync
	cd web && npm install

test:               ## Solidity tests + Python tests (Python chain tests use a local anvil node)
	cd contracts && forge test
	cd backend && uv run pytest -q

abi:                ## rebuild contracts and export ABIs to backend/app/chain/abi
	cd contracts && ./script/export_abi.sh

gate-m2:            ## M2 gate: one real x402 payment, agent -> demo vendor, on Base Sepolia
	cd backend && uv run python -m scripts.gate_m2

gate-m2-perps:      ## same gate, buying from the perps demo vendor (Hyperliquid data) instead of Deribit
	cd backend && uv run python -m scripts.gate_m2 --seller perps
