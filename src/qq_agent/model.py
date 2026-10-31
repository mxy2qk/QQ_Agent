"""SDK objects terminate at this boundary; the graph uses plain response dataclasses."""

import json
from dataclasses import dataclass, field
from typing import Protocol

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from qq_agent.config import Settings


class ModelError(RuntimeError):
    pass


class ModelInterruptedError(ModelError):
    def __init__(self):
        super().__init__("模型服务临时中断，本次未完成，请稍后重试。")


def is_interruption_marker(content):
    return isinstance(content, str) and content.strip().casefold() in {
        "[用户中断]",
        "【用户中断】",
        "[user interrupted]",
        "模型服务临时中断，此轮未完成。",
        "模型服务临时中断，本次未完成，请稍后重试。",
    }


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ModelResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    reasoning_content: str | None = None

    def as_message(self):
        result = {"role": "assistant", "content": self.content or None}
        if self.reasoning_content is not None:
            result["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            result["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments},
                }
                for c in self.tool_calls
            ]
        return result


class LLMClient(Protocol):
    async def generate(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | dict = "auto",
    ) -> ModelResponse: ...

    async def close(self) -> None: ...


class DeepSeekClient:
    def __init__(self, settings: Settings):
        settings.require_live()
        self.model = settings.deepseek_model
        self.client = AsyncOpenAI(
            api_key=settings.deepseek_api_key.get_secret_value(),
            base_url=settings.deepseek_base_url,
            timeout=settings.model_timeout_seconds,
            max_retries=0,
        )

    async def generate(self, messages, tools=None, tool_choice="auto"):
        kwargs = {
            "model": self.model,
            "messages": messages,
            "extra_body": {"thinking": {"type": "disabled"}},
        }
        if tools:
            kwargs.update(tools=tools, tool_choice=tool_choice)
        try:
            response = await self.client.chat.completions.create(**kwargs)
        except APITimeoutError:
            raise ModelError("模型请求超时，请稍后重试") from None
        except APIConnectionError:
            raise ModelError("无法连接模型服务，请检查网络与 BASE_URL") from None
        except APIStatusError as exc:
            hints = {401: "API Key 无效", 402: "账户余额不足", 429: "模型服务限流"}
            raise ModelError(
                hints.get(exc.status_code, f"模型服务请求失败（HTTP {exc.status_code}）")
            ) from None
        if not response.choices:
            raise ModelError("模型返回空 choices")
        choice = response.choices[0]
        if choice.finish_reason in {"length", "content_filter"}:
            raise ModelError("模型回答被截断或被服务过滤，请缩短问题或重试")
        msg = choice.message
        if getattr(msg, "refusal", None):
            raise ModelError("模型拒绝了本次请求")
        calls = [
            ToolCall(c.id, c.function.name, c.function.arguments) for c in (msg.tool_calls or [])
        ]
        if any(not c.id or not c.name for c in calls) or len({c.id for c in calls}) != len(calls):
            raise ModelError("模型返回无效的工具调用标识")
        if not msg.content and not calls:
            raise ModelError("模型返回空回答")
        if not calls and is_interruption_marker(msg.content):
            raise ModelInterruptedError()
        return ModelResponse(
            msg.content or "",
            calls,
            response.usage.model_dump() if response.usage else {},
            getattr(msg, "reasoning_content", None),
        )

    async def close(self):
        await self.client.close()


class DemoClient:
    """Explicit offline demo, not evidence of real model capabilities."""

    async def generate(self, messages, tools=None, tool_choice="auto"):
        last = messages[-1]
        if last["role"] == "tool":
            data = json.loads(last["content"])
            return ModelResponse("[离线演示] " + json.dumps(data, ensure_ascii=False))
        return ModelResponse("[离线演示] 你好！真实对话需填写 .env；实时信息需要可用工具支持。")

    async def close(self):
        pass
