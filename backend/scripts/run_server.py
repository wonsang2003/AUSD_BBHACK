"""
Start the PocketDesk backend (FastAPI app `app.main:app`) with uvicorn.

    cd backend && uv run python -m scripts.run_server                                 # repo-root .env
    cd backend && uv run python -m scripts.run_server --env-file ../.env.local-anvil  # offline anvil stack
    cd backend && uv run python -m scripts.run_server --port 8001 --reload

`--env-file` loads extra settings ON TOP of the repo-root .env (its values win), e.g. the
RPC / AUSD / controller / faucet overrides written by `scripts/local_stack.py`. The variables
are put into this process's environment before the app is imported, so `get_settings()` (and
uvicorn's reload workers) see them. Only variable NAMES are printed, never values.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import uvicorn
from dotenv import dotenv_values

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
APP_IMPORT_PATH = "app.main:app"


def load_env_file(path: Path) -> list[str]:
    """Copy every assignment in `path` into os.environ (overriding); returns the variable names."""
    if not path.is_file():
        sys.exit(f"--env-file {path} does not exist")
    values = {name: value for name, value in dotenv_values(path).items() if value is not None}
    os.environ.update(values)
    return sorted(values)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", type=Path, help="extra .env file whose values override the repo-root .env")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--reload", action="store_true", help="restart on code changes (development)")
    args = parser.parse_args()

    if args.env_file:
        names = load_env_file(args.env_file.resolve())
        print(f"loaded {len(names)} settings from {args.env_file}: {', '.join(names)}")
    print(f"PocketDesk API on http://{args.host}:{args.port}  (docs: /docs, health: /api/health)")
    uvicorn.run(APP_IMPORT_PATH, host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
