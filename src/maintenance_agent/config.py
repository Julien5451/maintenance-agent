from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    llm_model: str = ""
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_timeout_seconds: float = Field(default=45, gt=0, le=180)
    database_dir: Path = ROOT / "runtime"

    @property
    def model_ready(self) -> bool:
        return bool(self.llm_model and self.llm_base_url and self.llm_api_key.get_secret_value())
