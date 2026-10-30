from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field


class IncomingMessage(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")
    platform: str = Field(min_length=1, max_length=64)
    message_id: str = Field(min_length=1, max_length=256)
    chat_type: Literal["private", "group"]
    chat_id: str = Field(min_length=1, max_length=256)
    user_id: str = Field(min_length=1, max_length=256)
    nickname: str | None = None
    text: str = Field(min_length=1, max_length=32000)
    is_mentioned: bool = False
    timestamp: str | None = None

    @property
    def thread_id(self):
        # Include the user in Phase 1: no shared group context or memory yet.
        return ":".join(
            quote(v, safe="") for v in (self.platform, self.chat_type, self.chat_id, self.user_id)
        )
