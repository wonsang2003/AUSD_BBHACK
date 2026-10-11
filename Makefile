# PocketDesk — common commands. Run from the repo root.
# On-chain targets need the repo-root .env (see .env.example) and network access to Base Sepolia.
# Offline alternative: `make local-stack` (anvil + MockAUSD + BudgetController), then
# `make server ENV_FILE=.env.local-anvil` and `make demo ENV_FILE=.env.local-anvil`.

FOUNDRY_BIN ?= $(HOME)/.foundry/bin
export PATH := $(FOUNDRY_BIN):$(PATH)

ENV_FILE ?=
ENV_FILE_ARG = $(if $(ENV_FILE),--env-file $(abspath $(ENV_FILE)),)
PORT ?= 8000

.PHONY: install test abi bootstrap deploy gate-m2 gate-m2-perps gate-m2-status \
	server web dev demo local-stack

install:            ## install backend + web dependencies
	cd backend && uv sync
	cd web && npm install

test:               ## Solidity tests + Python tests (Python chain tests use a local anvil node)
	cd contracts && forge test
	cd backend && uv run pytest -q

abi:                ## rebuild contracts and export ABIs to backend/app/chain/abi
	cd contracts && ./script/export_abi.sh

bootstrap:          ## create testnet wallets in .env (addresses printed, keys never printed) + on-chain checks
	cd backend && uv run python -m scripts.bootstrap

deploy:             ## deploy BudgetController to Base Sepolia (keeper = relayer); prints the address for .env
	@if [ -f backend/scripts/deploy.py ]; then \
		cd backend && uv run python -m scripts.deploy; \
	else \
		set -a && . ./.env && set +a && \
		export KEEPER_ADDRESS=$$(cast wallet address --private-key "$$RELAYER_PRIVATE_KEY") && \
		cd contracts && forge script script/Deploy.s.sol:Deploy --rpc-url "$$RPC_URL_BASE_SEPOLIA" --broadcast; \
	fi

gate-m2:            ## M2 gate: one real x402 payment, agent -> demo vendor, on Base Sepolia
	cd backend && uv run python -m scripts.gate_m2

gate-m2-perps:      ## same gate, buying from the perps demo vendor (Hyperliquid data) instead of Deribit
	cd backend && uv run python -m scripts.gate_m2 --seller perps

gate-m2-status:     ## show the recorded M2 payment (tx hash + Basescan link), or say M2 has not passed
	@cat backend/gate_m2_result.json 2>/dev/null \
		|| echo "M2 NOT PASSED: no backend/gate_m2_result.json yet. Run 'make gate-m2' (or 'make gate-m2-perps')."

server:             ## run the backend API (+ demo vendors) on :$(PORT); ENV_FILE=.env.local-anvil for the local stack
	cd backend && uv run python -m scripts.run_server --port $(PORT) $(ENV_FILE_ARG)

web:                ## run the Next.js dashboard on :3000
	cd web && npm run dev

dev:                ## backend + web together (Ctrl-C stops both)
	@trap 'kill 0' INT TERM EXIT; \
		(cd backend && uv run python -m scripts.run_server --port $(PORT) $(ENV_FILE_ARG)) & \
		(cd web && npm run dev) & \
		wait

demo:               ## end-to-end demo: faucet -> budget -> allocation -> recharge -> purchases -> finding -> sweep
	cd backend && uv run python -m scripts.demo_e2e $(ENV_FILE_ARG)

local-stack:        ## offline demo chain: anvil (84532) + MockAUSD + BudgetController, writes .env.local-anvil
	cd backend && uv run python -m scripts.local_stack
