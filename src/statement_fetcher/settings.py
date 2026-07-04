from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["sandbox", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    plaid_env: Environment = "sandbox"
    plaid_client_id: str | None = None
    plaid_secret: str | None = None
    plaid_language: str = "en-US"
    plaid_country_codes: str = "US"
    plaid_products: str = "statements"
    plaid_redirect_uri: str = "http://localhost:8765/plaid/callback"

    config_root: Path = Field(default=Path("config"), alias="PSF_CONFIG_ROOT")
    retry_max_attempts: int = Field(default=5, alias="PSF_RETRY_MAX_ATTEMPTS")
    retry_base_delay_seconds: float = Field(default=1.0, alias="PSF_RETRY_BASE_DELAY_SECONDS")
    retry_max_delay_seconds: float = Field(default=30.0, alias="PSF_RETRY_MAX_DELAY_SECONDS")

    @property
    def env_root(self) -> Path:
        return self.config_root / self.plaid_env

    @property
    def configuration_path(self) -> Path:
        return self.env_root / "configuration.json"

    @property
    def state_path(self) -> Path:
        return self.env_root / "state.json"

    @property
    def output_dir(self) -> Path:
        return self.env_root / "output"

    def load_credentials_fallback(self, credentials_path: Path = Path("credentials.json")) -> None:
        if self.plaid_client_id and self.plaid_secret:
            return
        if not credentials_path.exists():
            return

        data = json.loads(credentials_path.read_text(encoding="utf-8"))
        if not self.plaid_client_id:
            self.plaid_client_id = data.get("client_id")
        if not self.plaid_secret:
            self.plaid_secret = (data.get("secrets") or {}).get(self.plaid_env)
