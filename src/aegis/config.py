"""Settings + data-residency boot guard.

The app refuses to start unless:
  - AEGIS_DATA_RESIDENCY_CONFIRMED is true (acknowledges US-only routing)
  - BEDROCK_MODEL_ID begins with "us." (regional inference profile, never "global.")

Both are gates enforced at first import. A failure here means a misconfigured
environment routed bank statements outside the US — that must never silently boot.
"""

from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DataResidencyError(RuntimeError):
    """Raised when residency invariants are violated. Refuses to boot."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Data residency
    aegis_data_residency_confirmed: bool = False

    # AWS Bedrock
    aws_region: str = "us-east-1"
    aws_access_key_id: SecretStr | None = None
    aws_secret_access_key: SecretStr | None = None
    bedrock_model_id: str = "us.anthropic.claude-sonnet-4-6"

    # Supabase
    supabase_url: str = ""
    supabase_service_key: SecretStr | None = None

    # Zoho
    zoho_client_id: str = ""
    zoho_client_secret: SecretStr | None = None
    zoho_refresh_token: SecretStr | None = None
    zoho_accounts_base: str = "https://accounts.zoho.com"
    zoho_api_base: str = "https://www.zohoapis.com"
    zoho_webhook_secret: SecretStr | None = None

    # Funder-reply webhook (mp Phase 10). HMAC-SHA256 over the raw body
    # with this secret. Missing -> the webhook returns 503 so an
    # accidental deploy without the secret can't silently fail open.
    funder_reply_webhook_secret: SecretStr | None = None

    # API auth
    api_bearer_token: SecretStr | None = None

    # Redis (arq)
    redis_url: str = "redis://localhost:6379"

    # App
    app_port: int = Field(default=5555, ge=1, le=65535)
    log_level: str = "INFO"

    # Storage backend selector. "memory" = in-process dict (tests + offline);
    # "supabase" = Postgres via supabase-py (production default).
    aegis_storage_backend: Literal["memory", "supabase"] = "supabase"

    # Where uploaded PDFs land before the worker picks them up. The worker
    # deletes the file in a finally block — nothing here is long-lived.
    aegis_upload_dir: Path = Field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "aegis-uploads"
    )

    # Hard cap on uploaded PDF size (per CLAUDE.md security rules).
    aegis_max_upload_bytes: int = Field(default=25 * 1024 * 1024, gt=0)

    # Hard cap on TOTAL bytes accepted in one multi-statement intake
    # request. Lets the operator drop 3-4 statements at once without
    # uncapping individual file size. Default 100 MB = 4 statements at
    # the per-file cap.
    aegis_max_intake_total_bytes: int = Field(default=100 * 1024 * 1024, gt=0)

    # OFAC SDN cache. Refresh window 24h, hard cutoff 7d (see scoring/ofac.py).
    # Cache file is created on first refresh; the parent dir is auto-mkdir'd
    # by the get_ofac_client dependency.
    aegis_ofac_cache_path: Path = Field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "aegis-ofac" / "sdn.json"
    )

    # Worker tuning
    aegis_worker_max_concurrent: int = Field(default=4, ge=1, le=32)
    aegis_worker_job_timeout: int = Field(
        default=600, ge=30, description="seconds; longer than typical parse to allow LLM retries"
    )

    # Per-page parser routing (mp Phase 6.5). When True, the pipeline
    # classifies each page and routes text-bearing pages through the
    # text extractor / image pages through vision, instead of using
    # the legacy whole-doc strategy choice. Off by default until the
    # corpus + token-cost data validates the per-deal savings.
    aegis_parser_page_routing: bool = False

    @field_validator("bedrock_model_id")
    @classmethod
    def _model_must_be_regional_us(cls, v: str) -> str:
        if not v.startswith("us."):
            raise DataResidencyError(
                f"BEDROCK_MODEL_ID must start with 'us.' (regional US inference profile); "
                f"got {v!r}. Bank statements must not transit non-US regions."
            )
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings, run boot guard, cache. Raises DataResidencyError on violation."""
    settings = Settings()
    if not settings.aegis_data_residency_confirmed:
        raise DataResidencyError(
            "AEGIS_DATA_RESIDENCY_CONFIRMED must be true. "
            "Refusing to boot until US-only data routing is acknowledged."
        )
    return settings
