from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "MediaSync"
    app_version: str = "0.1.0"
    environment: str = "development"
    api_prefix: str = "/api/v1"
    database_url: str = "sqlite:///./data/mediasync.db"
    secret_key: str = Field(default="change-me-in-production", min_length=16)
    credential_encryption_key: str = Field(default="change-me-in-production", min_length=16)
    admin_username: str = "admin"
    admin_password: str = Field(default="admin", min_length=5)
    session_max_age_seconds: int = 86400
    scheduler_enabled: bool = True
    transfer_poll_seconds: int = 10
    transfer_batch_size: int = 2
    transfer_retry_base_seconds: int = Field(default=30, ge=5, le=3600)
    transfer_retry_max_seconds: int = Field(default=900, ge=30, le=86400)
    worker_poll_seconds: float = Field(default=1, gt=0, le=60)
    worker_lease_seconds: int = Field(default=60, ge=5, le=3600)
    worker_heartbeat_seconds: int = Field(default=20, ge=1, le=1800)
    worker_recovery_batch_size: int = Field(default=100, ge=1, le=1000)
    worker_retry_base_seconds: int = Field(default=30, ge=1, le=3600)
    worker_retry_max_seconds: int = Field(default=900, ge=1, le=86400)
    cors_origins: str = "http://localhost:5173,http://localhost:8080"
    log_level: str = "INFO"
    aliyundrive_mode: str = "private_api"
    aliyundrive_api_base_url: str = "https://openapi.alipan.com"
    aliyundrive_client_id: str = ""
    aliyundrive_client_secret: str = ""
    aliyundrive_private_api_base_url: str = "https://api.alipan.com"
    aliyundrive_private_auth_base_url: str = "https://auth.alipan.com"
    aliyundrive_qr_login_base_url: str = "https://passport.aliyundrive.com"
    aliyundrive_request_interval_seconds: float = Field(default=0.8, ge=0, le=10)
    aliyundrive_request_jitter_seconds: float = Field(default=0.3, ge=0, le=5)
    aliyundrive_request_max_retries: int = Field(default=3, ge=0, le=8)
    aliyundrive_retry_backoff_seconds: float = Field(default=2, ge=0, le=30)
    aliyundrive_retry_max_seconds: float = Field(default=30, ge=1, le=300)
    scheduler_jitter_seconds: int = Field(default=120, ge=0, le=900)
    manual_scan_cooldown_seconds: int = Field(default=60, ge=0, le=3600)
    folder_scan_batch_size: int = Field(default=20, ge=1, le=500)
    full_scan_interval_hours: int = Field(default=24, ge=1, le=720)

    @model_validator(mode="after")
    def validate_worker_settings(self) -> "Settings":
        if self.worker_heartbeat_seconds >= self.worker_lease_seconds:
            raise ValueError(
                "worker_heartbeat_seconds must be shorter than worker_lease_seconds"
            )
        if self.worker_retry_max_seconds < self.worker_retry_base_seconds:
            raise ValueError(
                "worker_retry_max_seconds must not be shorter than "
                "worker_retry_base_seconds"
            )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [value.strip() for value in self.cors_origins.split(",") if value.strip()]

    def ensure_data_directories(self) -> None:
        if not self.database_url.startswith("sqlite:///"):
            return
        database_path = self.database_url.removeprefix("sqlite:///")
        if database_path == ":memory:":
            return
        Path(database_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
