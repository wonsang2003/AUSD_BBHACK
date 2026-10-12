"""
Runtime configuration, read from environment variables / the repo-root `.env`.

Use `get_settings()` everywhere instead of reading `os.environ` directly, so the
whole app sees one consistent, validated configuration.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.constants import (
    AUSD_FAUCET_ADDRESS,
    AUSD_TESTNET_ADDRESS,
    BASE_SEPOLIA_CHAIN_ID,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ─── chain ───────────────────────────────────────────────────────────
    rpc_url_base_sepolia: str = "https://sepolia.base.org"
    chain_id: int = BASE_SEPOLIA_CHAIN_ID
    ausd_address: str = AUSD_TESTNET_ADDRESS
    ausd_faucet: str = AUSD_FAUCET_ADDRESS
    budget_controller_address: str = ""

    # hot keys — TESTNET ONLY, never logged, never sent to the browser
    relayer_private_key: str = ""
    seller_settler_private_key: str = ""
    agent_onchainperps_key: str = ""
    agent_optionsevent_key: str = ""
    agent_newssentiment_key: str = ""
    agent_macrofilings_key: str = ""
    demo_user_private_key: str = ""

    # seller payout addresses (one wallet per "independent analyst" seller)
    seller_payto_expected_move: str = ""
    seller_payto_pm_edge: str = ""
    seller_payto_perps: str = ""
    seller_payto_news: str = ""
    seller_payto_insider: str = ""
    seller_payto_macro: str = ""

    # x402 settlement: "self" = our seller submits transferWithAuthorization;
    # "facilitator" = POST to an x402 facilitator's /verify and /settle.
    x402_settlement: str = "self"
    x402_facilitator_url: str = "https://x402.org/facilitator"

    # ─── data sources ────────────────────────────────────────────────────
    sec_user_agent: str = "PocketDesk your-email@example.com"
    context_dev_api_key: str = ""
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-5-5"

    # ─── live vendor mode (mainnet USDC) — off by default ────────────────
    live_vendor_mode: bool = False
    live_vendor_private_key: str = ""
    live_vendor_max_total_usd: float = 1.00
    live_vendor_max_per_call_usd: float = 0.10

    # ─── alerts ──────────────────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ─── server ──────────────────────────────────────────────────────────
    public_base_url: str = "http://localhost:8000"
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    database_url: str = f"sqlite:///{BACKEND_DIR / 'pocketdesk.db'}"

    # Skip on-chain startup assertions (unit tests / offline UI work only).
    skip_chain_checks: bool = False

    def agent_keys(self) -> dict[str, str]:
        """Agent name → private key, in the canonical agent order."""
        return {
            "OnchainPerps": self.agent_onchainperps_key,
            "OptionsEvent": self.agent_optionsevent_key,
            "NewsSentiment": self.agent_newssentiment_key,
            "MacroFilings": self.agent_macrofilings_key,
        }

    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
