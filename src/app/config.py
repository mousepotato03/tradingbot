from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Values accepted by the OpenAI Responses `reasoning.effort` field; support varies by model.
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRADINGBOT_", env_file=".env", extra="ignore")
    mode: str = "fixture"
    database_url: str = Field(default="sqlite:///.research/research.db", repr=False)
    openai_api_key: SecretStr = SecretStr("")
    research_model: str = ""
    pm_model: str = ""
    # Cheap classification model for discovery triage; falls back to research_model.
    triage_model: str = ""
    # None keeps the provider default reasoning effort.
    research_reasoning_effort: ReasoningEffort | None = None
    pm_reasoning_effort: ReasoningEffort | None = None
    triage_reasoning_effort: ReasoningEffort | None = None
    toss_client_id: SecretStr = SecretStr("")
    toss_client_secret: SecretStr = SecretStr("")
    toss_account_seq: SecretStr = SecretStr("")
    # Tavily's free plan needs no card and blocks at its limit; Brave bills beyond its credit.
    search_provider: Literal["tavily", "brave"] = "tavily"
    tavily_api_key: SecretStr = SecretStr("")
    brave_api_key: SecretStr = SecretStr("")
    # Live searches allowed in any rolling 30 days; None removes the local cap.
    search_monthly_limit: int | None = Field(default=900, ge=0)
    sec_user_agent: str = ""
    discord_webhook: SecretStr = SecretStr("")
    artifact_dir: Path = Path(".research/artifacts")
    quote_max_age_seconds: int = 90
    portfolio_max_age_seconds: int = 300
    # Latest completed daily bar may be a long weekend old; older bars make derived levels stale.
    ohlcv_max_age_seconds: int = 5 * 86400
    # Shared account snapshot reuse window inside one process (monitor and research reads).
    account_cache_seconds: int = 60
    monitor_account_interval_seconds: int = 300
    # Per-minute quote/account monitor reads are deleted after this many days.
    observation_retention_days: int = Field(default=14, ge=1)
    # USD holdings found in the account are watched and researched automatically.
    holdings_auto_watch: bool = True
    # Mode for watched holdings research; quick keeps model and search usage low.
    holding_research_mode: Literal["quick", "normal", "deep", "critical"] = "quick"
    # Routine re-research: "market_open" runs on trading days at the regular open plus an offset
    # (fresh regular-session quotes are needed to validate levels); "interval" every N hours.
    research_schedule: Literal["market_open", "interval"] = "market_open"
    market_open_offset_minutes: int = 10
    # market_open schedule: routine re-research every N trading days; events still trigger
    # research immediately in between.
    routine_research_trading_days: int = Field(default=3, ge=1)
    research_interval_hours: int = Field(default=24, ge=1)
    max_output_tokens: int = 16000
    worker_lease_seconds: int = 7200
    discovery_batch: int = 5
    discovery_enabled: bool = False
    # Deterministic screen before model triage. Liquidity/size/data are gates; trend only ranks.
    discovery_screen_limit: int = 150
    discovery_triage_limit: int = 20
    discovery_rescreen_days: int = 30
    discovery_min_price: Decimal = Decimal("5")
    discovery_min_dollar_volume: Decimal = Decimal("20000000")
    discovery_min_market_cap: Decimal = Decimal("2000000000")
    # Depositary receipts need an ADS ratio for per-share work; ETFs use a separate path.
    discovery_security_types: list[str] = ["STOCK", "FOREIGN_STOCK", "REIT"]
    benchmark_ticker: str = "SPY"
    retained_document_hosts: list[str] = [
        "www.sec.gov",
        "sec.gov",
        "data.sec.gov",
        "www.federalreserve.gov",
        "www.bls.gov",
        "www.bea.gov",
    ]

    @field_validator(
        "research_reasoning_effort",
        "pm_reasoning_effort",
        "triage_reasoning_effort",
        "search_monthly_limit",
        mode="before",
    )
    @classmethod
    def unset_optional(cls, value):
        # `.env` lines left blank arrive as empty strings; empty means the default behavior.
        return None if value == "" else value

    def require_live(self) -> None:
        if self.mode != "live":
            raise ValueError("Live adapter construction requires TRADINGBOT_MODE=live")
        required = {
            "OPENAI_API_KEY": self.openai_api_key.get_secret_value(),
            "RESEARCH_MODEL": self.research_model,
            "PM_MODEL": self.pm_model,
            "TOSS_CLIENT_ID": self.toss_client_id.get_secret_value(),
            "TOSS_CLIENT_SECRET": self.toss_client_secret.get_secret_value(),
            "SEC_USER_AGENT": self.sec_user_agent,
        }
        if self.search_provider == "tavily":
            required["TAVILY_API_KEY"] = self.tavily_api_key.get_secret_value()
        else:
            required["BRAVE_API_KEY"] = self.brave_api_key.get_secret_value()
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ValueError("Missing live configuration: " + ", ".join(missing))
