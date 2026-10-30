import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from qq_agent.model import ToolCall

logger = logging.getLogger("qq_agent")


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TimeArguments(ToolArguments):
    timezone: str = Field(
        default="Asia/Shanghai", max_length=128, description="IANA 时区，例如 Asia/Shanghai 或 UTC"
    )


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    arguments: type[ToolArguments]
    handler: Callable[..., Awaitable[dict]]
    side_effect: bool = False

    def schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.arguments.model_json_schema(),
            },
        }


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        if tool.name in self._tools:
            raise ValueError("工具重复注册")
        # All Phase 1 tools must be cancellable asynchronous, read-only functions.
        if not asyncio.iscoroutinefunction(tool.handler):
            raise ValueError("工具必须使用 async 函数")
        if tool.side_effect:
            raise ValueError("Phase 1 禁止注册有外部副作用的工具")
        self._tools[tool.name] = tool

    def schemas(self):
        return [tool.schema() for tool in self._tools.values()]

    def get(self, name):
        return self._tools.get(name)


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, timeout: float):
        self.registry, self.timeout = registry, timeout

    async def execute(self, call: ToolCall):
        started = time.monotonic()
        tool = self.registry.get(call.name)
        code = None
        result = None
        try:
            if tool is None:
                code = "unknown_tool"
            else:
                args = tool.arguments.model_validate_json(call.arguments)
                async with asyncio.timeout(self.timeout):
                    result = await tool.handler(**args.model_dump())
                # Serialization errors are tool failures too, not runtime crashes.
                json.dumps(result, allow_nan=False)
        except ValidationError:
            code = "invalid_arguments"
        except TimeoutError:
            code = "tool_timeout"
        except Exception:  # noqa: BLE001 -- untrusted tool boundary, never echo raw errors
            code = "tool_failed"
        logger.info(
            "tool_completed",
            extra={
                "fields": {
                    "tool_name": tool.name if tool else "<unregistered>",
                    "status": "error" if code else "ok",
                    "error_type": code,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                }
            },
        )
        return {"ok": False, "error": code} if code else {"ok": True, "data": result}


def default_registry(timezone="Asia/Shanghai"):
    configured_timezone = timezone

    async def get_current_time(timezone: str = timezone):
        return {"datetime": datetime.now(ZoneInfo(timezone)).isoformat(), "timezone": timezone}

    # Use configured timezone for both schema and handler defaults.
    class ConfiguredTimeArguments(TimeArguments):
        timezone: str = Field(default=configured_timezone, max_length=128, description="IANA 时区")

    registry = ToolRegistry()
    registry.register(
        Tool(
            "get_current_time",
            "查询指定时区的真实当前时间；普通问候无需调用。",
            ConfiguredTimeArguments,
            get_current_time,
        )
    )
    return registry
