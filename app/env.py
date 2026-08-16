from collections.abc import Sequence
from functools import lru_cache
from typing import Annotated

from pydantic import BeforeValidator, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from pydantic_settings.exceptions import SettingsError


def _parse_cors_allowed_origins(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [str(item).strip() for item in value if str(item).strip()]
    raise TypeError("cors_allowed_origins must be a list or comma-separated string")


CorsAllowedOrigins = Annotated[
    list[str], NoDecode, BeforeValidator(_parse_cors_allowed_origins)
]


class Env(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env.local",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    supabase_url: str
    supabase_secret_key: SecretStr = Field(min_length=1)
    smartlead_api_key: SecretStr = Field(min_length=1)
    leadmagic_api_key: SecretStr = Field(min_length=1)
    prospeo_api_key: SecretStr = Field(min_length=1)
    airscale_api_key: SecretStr = Field(min_length=1)
    fullenrich_api_key: SecretStr = Field(min_length=1)
    internal_api_token: SecretStr = Field(min_length=32)
    public_api_base_url: str
    fullenrich_webhook_token: SecretStr = Field(min_length=32)
    cors_allowed_origins: CorsAllowedOrigins = Field(default_factory=list)
    invite_redirect_url: str | None = None
    smartlead_base_url: str = "https://server.smartlead.ai/api/v1"
    smartlead_timeout_seconds: float = Field(default=30.0, gt=0)
    smartlead_max_retries: int = Field(default=3, ge=0, le=10)
    smartlead_import_limit: int = Field(default=1000, gt=0, le=10_000)
    leadmagic_base_url: str = "https://api.leadmagic.io"
    prospeo_base_url: str = "https://api.prospeo.io"
    airscale_base_url: str = "https://api.airscale.io"
    fullenrich_base_url: str = "https://app.fullenrich.com/api/v2"
    phone_provider_timeout_seconds: float = Field(default=30.0, gt=0)
    phone_provider_max_retries: int = Field(default=2, ge=0, le=5)
    phone_enrichment_concurrency: int = Field(default=5, ge=1, le=20)
    phone_enrichment_reconcile_seconds: int = Field(default=300, ge=300, le=3600)


@lru_cache
def get_env() -> Env:
    return Env()


def load_cors_allowed_origins() -> list[str]:
    try:
        return get_env().cors_allowed_origins
    except (ValidationError, SettingsError):
        return []
