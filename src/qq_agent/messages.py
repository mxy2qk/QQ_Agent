from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field


class GroupMember(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    user_id: str = Field(min_length=1, max_length=256)
    nickname: str = Field(default="", max_length=256)
    card: str = Field(default="", max_length=256)


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
    public_response: bool = False
    group_members: tuple[GroupMember, ...] = Field(default=(), max_length=100)
    mentioned_users: tuple[str, ...] = Field(default=(), max_length=100)

    @property
    def thread_id(self):
        # Conversation snapshots remain per member; authorized group excerpts
        # are supplied separately at request time and never join private threads.
        return ":".join(
            quote(v, safe="") for v in (self.platform, self.chat_type, self.chat_id, self.user_id)
        )
