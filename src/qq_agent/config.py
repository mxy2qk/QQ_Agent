from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(ValueError):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = ""
    deepseek_model: str = ""
    max_agent_steps: int = Field(default=5, ge=1, le=100)
    max_tool_calls: int = Field(default=5, ge=1, le=100)
    tool_timeout_seconds: float = Field(default=30, gt=0, le=3600)
    agent_timeout_seconds: float = Field(default=120, gt=0, le=3600)
    model_timeout_seconds: float = Field(default=60, gt=0, le=3600)
    timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"
    channel_mode: str = "cli"
    qq_adapter_mode: str = "unselected"

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("TIMEZONE 必须是有效的 IANA 时区") from None
        return value

    @field_validator("log_level")
    @classmethod
    def valid_log_level(cls, value):
        value = value.upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL 无效")
        return value

    def require_live(self):
        missing = []
        for name in ("deepseek_api_key", "deepseek_base_url", "deepseek_model"):
            value = getattr(self, name)
            value = value.get_secret_value() if isinstance(value, SecretStr) else value
            if not value.strip() or value in {"replace_me", "your_api_key"}:
                missing.append(name.upper())
        if missing:
            raise ConfigurationError("请在 .env 或环境变量中填写：" + ", ".join(missing))
        url = urlsplit(self.deepseek_base_url)
        if url.scheme != "https" or not url.netloc or url.username or url.password:
            raise ConfigurationError("DEEPSEEK_BASE_URL 必须是无内嵌凭据的 HTTPS 地址")
        if self.channel_mode != "cli":
            raise ConfigurationError("当前阶段仅支持 CHANNEL_MODE=cli")
