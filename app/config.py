from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    app_name: str = "REGOFIS bot API"
    admin_upload_token: str = "CHANGE_ME_BEFORE_RUNNING"
    max_upload_mb: int = 15

    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

settings = Settings()
DATA_DIR = ROOT_DIR / "data"
GENERATED_DIR = DATA_DIR / "generated"
FRONTEND_DIR = ROOT_DIR / "frontend"
