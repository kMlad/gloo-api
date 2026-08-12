from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Env(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env.local",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    supabase_url: str
    supabase_secret_key: SecretStr = Field(min_length=1)
    smartlead_api_key: SecretStr = Field(min_length=1)
    internal_api_token: SecretStr = Field(min_length=32)
    smartlead_base_url: str = "https://server.smartlead.ai/api/v1"
    smartlead_timeout_seconds: float = Field(default=30.0, gt=0)
    smartlead_max_retries: int = Field(default=3, ge=0, le=10)
    smartlead_import_limit: int = Field(default=1000, gt=0, le=10_000)


@lru_cache
def get_env() -> Env:
    return Env()
