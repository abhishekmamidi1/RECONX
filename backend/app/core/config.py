from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = Field(default="Recon Engine", alias="APP_NAME")
    app_version: str = Field(default="0.1.0", alias="APP_VERSION")
    env: str = Field(default="development", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    postgres_host: str = Field(default="localhost", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    postgres_user: str = Field(alias="POSTGRES_USER")
    postgres_password: str = Field(alias="POSTGRES_PASSWORD")
    postgres_db: str = Field(default="reconciliation", alias="POSTGRES_DB")

    cors_origins: str = Field(
        default="http://localhost:5173", alias="CORS_ORIGINS"
    )
    max_upload_size_mb: int = Field(default=25, alias="MAX_UPLOAD_SIZE_MB")
    default_actor: str = Field(default="system", alias="DEFAULT_ACTOR")

    embedding_provider: str = Field(default="hashing", alias="EMBEDDING_PROVIDER")
    embedding_api_url: str = Field(
        default="http://localhost:11434", alias="EMBEDDING_API_URL"
    )
    embedding_model: str = Field(default="bge-m3", alias="EMBEDDING_MODEL")
    reasoning_provider: str = Field(default="heuristic", alias="REASONING_PROVIDER")
    reasoning_api_url: str = Field(
        default="http://localhost:11434", alias="REASONING_API_URL"
    )
    reasoning_model: str = Field(default="qwen3:0.6b", alias="REASONING_MODEL")
    reasoning_timeout_s: float = Field(default=60.0, alias="REASONING_TIMEOUT_S")
    semantic_index_path: str = Field(
        default="/app/data/semantic_index.faiss", alias="SEMANTIC_INDEX_PATH"
    )
    erp_webhook_url: str = Field(default="", alias="ERP_WEBHOOK_URL")

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024

    @property
    def cors_origin_list(self) -> list[str]:
        raw = self.cors_origins.strip()
        if raw.startswith("["):
            import json

            parsed = json.loads(raw)
            return [str(origin) for origin in parsed]
        return [origin.strip() for origin in raw.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
