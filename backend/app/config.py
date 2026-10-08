from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    redis_url: str | None = None
    app_mode: Literal["local", "production"] = "production"
    jwt_secret: str
    jwt_expire_minutes: int = 480
    # Stage 3: file storage
    upload_dir: str = "/data/uploads"
    max_upload_mb: int = 25
    # Stage 4: certificates (defaults mean no .env change is needed)
    cert_secret: str = ""  # HMAC key; falls back to a key derived from jwt_secret
    public_base_url: str = "http://localhost:8000"  # what the certificate QR points to

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
