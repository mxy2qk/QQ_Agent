import ipaddress
from urllib.parse import urlsplit

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
    log_level: str = "INFO"
    channel_mode: str = "cli"
    qq_adapter_mode: str = "unselected"
    database_path: str = "data/agent.db"
    checkpoint_db_path: str = "data/checkpoints.db"
    message_retention_days: int = Field(default=7, ge=1, le=3650)
    memory_extraction_enabled: bool = True
    public_sources_enabled: bool = False
    source_timeout_seconds: float = Field(default=15, gt=0, le=60)
    source_interval_seconds: float = Field(default=2, ge=1, le=60)
    qq_ws_url: str = ""
    qq_access_token: SecretStr = SecretStr("")
    qq_self_id: str = ""
    qq_allowed_groups: list[str] = Field(default_factory=list)
    qq_allowed_users: list[str] = Field(default_factory=list)
    qq_private_enabled: bool = False
    qq_live_enabled: bool = False
    qq_action_timeout_seconds: float = Field(default=15, gt=0, le=120)
    qq_reply_interval_seconds: float = Field(default=3, ge=0, le=300)
    qq_reconnect_max_seconds: float = Field(default=30, ge=1, le=300)

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
        if self.channel_mode not in {"cli", "qq"}:
            raise ConfigurationError("CHANNEL_MODE 必须为 cli 或 qq")

    def require_qq(self):
        if self.qq_adapter_mode != "onebot" or not self.qq_live_enabled:
            raise ConfigurationError("QQ 接入须设置 QQ_ADAPTER_MODE=onebot 和 QQ_LIVE_ENABLED=true")
        if not self.qq_self_id.isdecimal() or int(self.qq_self_id) <= 0:
            raise ConfigurationError("请填写 QQ_SELF_ID")
        if not self.qq_access_token.get_secret_value().strip():
            raise ConfigurationError("请填写本地 QQ_ACCESS_TOKEN，不要发送到聊天中")
        u = urlsplit(self.qq_ws_url)
        if (
            u.scheme not in {"ws", "wss"}
            or not u.hostname
            or u.username
            or u.query
            or u.fragment
            or u.path not in {"", "/"}
        ):
            raise ConfigurationError(
                "QQ_WS_URL 必须是 OneBot 正向 WS 根地址，不含内嵌凭据或查询参数"
            )
        if u.scheme == "ws":
            try:
                local = ipaddress.ip_address(u.hostname).is_loopback
            except ValueError:
                local = u.hostname == "localhost"
            if not local:
                raise ConfigurationError("明文 ws 仅支持本机回环地址，远程连接请使用 wss")
        if not self.qq_allowed_groups and not (self.qq_private_enabled and self.qq_allowed_users):
            raise ConfigurationError("请配置允许的群或私聊用户白名单")
        if any(
            not x.isdecimal() or int(x) <= 0
            for x in [*self.qq_allowed_groups, *self.qq_allowed_users]
        ):
            raise ConfigurationError("QQ 白名单必须是正整数 ID 字符串数组")
