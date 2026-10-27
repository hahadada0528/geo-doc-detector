from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ark_api_key: str = ""
    ark_model: str = "ep-m-20260731121317-fpf4c"
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    ask_interval_sec: float = 2.0
    database_url: str = f"sqlite+aiosqlite:///{(ROOT / 'data' / 'geo.db').as_posix()}"
    session_secret: str = "geo-local-session-secret-change-me"

    # Hermes / wiki-assistant（OpenAI 兼容，默认本机 8080）
    hermes_base_url: str = "http://127.0.0.1:8080"
    hermes_api_key: str = ""
    hermes_model: str = "wiki-assistant"
    hermes_timeout_sec: float = 300.0

    # 知识库上传（multipart POST /v1/files）
    kb_base_url: str = "http://127.0.0.1:8686"
    kb_api_key: str = ""
    kb_purpose: str = "brand-extras"
    kb_timeout_sec: float = 120.0


settings = Settings()
