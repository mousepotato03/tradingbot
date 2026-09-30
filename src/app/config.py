from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRADINGBOT_", env_file=".env", extra="ignore")
    mode: str = "fixture"
    database_url: str = Field(default="sqlite:///.research/research.db", repr=False)
    database_host: str = ""
    openai_api_key: SecretStr = SecretStr("")
    research_model: str = ""
    pm_model: str = ""
    toss_client_id: SecretStr = SecretStr("")
    toss_client_secret: SecretStr = SecretStr("")
    toss_account_seq: SecretStr = SecretStr("")
    brave_api_key: SecretStr = SecretStr("")
    sec_user_agent: str = ""
    browser_url: str = "http://browser-worker:8001"
    browser_token: SecretStr = SecretStr("")
    discord_webhook: SecretStr = SecretStr("")
    egress_proxy: str = ""
    postgres_password: SecretStr = SecretStr("")
    artifact_dir: Path = Path(".research/artifacts")
    quote_max_age_seconds: int = 90
    portfolio_max_age_seconds: int = 300
    max_output_tokens: int = 16000
    worker_lease_seconds: int = 7200
    discovery_batch: int = 5
    discovery_enabled: bool = False
    benchmark_ticker: str = "SPY"
    retained_document_hosts: list[str] = [
        "www.sec.gov",
        "sec.gov",
        "data.sec.gov",
        "www.federalreserve.gov",
        "www.bls.gov",
        "www.bea.gov",
    ]
    retain_browser_screenshots: bool = False

    @model_validator(mode="after")
    def database_connection(self):
        if self.database_host:
            from sqlalchemy import URL

            self.database_url = URL.create(
                "postgresql+psycopg",
                username="research",
                password=self.postgres_password.get_secret_value(),
                host=self.database_host,
                database="research",
            ).render_as_string(hide_password=False)
        return self

    def require_live(self) -> None:
        if self.mode != "live":
            raise ValueError("Live adapter construction requires TRADINGBOT_MODE=live")
        required = {
            "OPENAI_API_KEY": self.openai_api_key.get_secret_value(),
            "RESEARCH_MODEL": self.research_model,
            "PM_MODEL": self.pm_model,
            "TOSS_CLIENT_ID": self.toss_client_id.get_secret_value(),
            "TOSS_CLIENT_SECRET": self.toss_client_secret.get_secret_value(),
            "BRAVE_API_KEY": self.brave_api_key.get_secret_value(),
            "SEC_USER_AGENT": self.sec_user_agent,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ValueError("Missing live configuration: " + ", ".join(missing))
