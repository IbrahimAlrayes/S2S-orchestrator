# Minimal Pydantic settings for the temp RAG adapter. The ported files'
# `from core.setting import settings` imports were rewritten to
# `from .settings_shim import settings` during the port — see README.md.

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class _RagSettings(BaseSettings):
    MILVUS_HOST: str = ""
    MILVUS_PORT: str = "19530"
    MILVUS_COLLECTION: str = ""
    MILVUS_TOKEN: str = ""
    EMBEDDING_SERVICE_URL: str = ""
    RERANK_SERVICE_URL: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=True)


settings = _RagSettings()
