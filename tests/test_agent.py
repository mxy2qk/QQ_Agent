import asyncio
import json

import pytest

from qq_agent.config import ConfigurationError, Settings
from qq_agent.messages import IncomingMessage
from qq_agent.model import ModelError, ModelResponse, ToolCall
from qq_agent.service import AgentService
from qq_agent.tools import Tool, ToolArguments, ToolExecutor, ToolRegistry, default_registry


class ScriptedClient:
    def __init__(self, *responses, delay=0):
        self.responses = list(responses)
        self.requests = []
        self.delay = delay

    async def generate(self, messages, tools=None, tool_choice="auto"):
        self.requests.append((list(messages), tools, tool_choice))
        await asyncio.sleep(self.delay)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        pass


def config(**kwargs):
    return Settings(_env_file=None, **kwargs)


def msg(text="你好", mid="1", user="a", chat="g", platform="cli"):
    return IncomingMessage(
        platform=platform, message_id=mid, chat_type="group", chat_id=chat, user_id=user, text=text
    )


def call(name="get_current_time", arguments="{}", cid="time-1"):
    return ModelResponse(tool_calls=[ToolCall(cid, name, arguments)])


def service(client, settings=None, registry=None):
    return AgentService(client, registry or default_registry(), settings or config())


async def test_greeting_and_multi_turn_context():
    client = ScriptedClient(ModelResponse("你好"), ModelResponse("你刚才问好了"))
    svc = service(client)
    first = await svc.handle(msg())
    second = await svc.handle(msg("我刚才说了什么", "2"))
    assert first.tool_calls == second.tool_calls == 0
    assert client.requests[1][0][1:3] == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好"},
    ]


async def test_real_time_tool_result_reaches_model():
    client = ScriptedClient(call(), ModelResponse("上海当前时间如下"))
    reply = await service(client).handle(msg("现在几点"))
    assert reply.status == "ok" and reply.tool_calls == 1 and reply.model_steps == 2
    tool = client.requests[1][0][-1]
    assert tool["role"] == "tool" and tool["tool_call_id"] == "time-1"
    result = json.loads(tool["content"])
    assert result["ok"] and result["data"]["timezone"] == "Asia/Shanghai"
    assert "+08:00" in result["data"]["datetime"]


@pytest.mark.parametrize(
    "name,arguments,error",
    [
        ("unregistered", "{}", "unknown_tool"),
        ("get_current_time", '{"extra":1}', "invalid_arguments"),
        ("get_current_time", '{"timezone":42}', "invalid_arguments"),
        ("get_current_time", "not-json", "invalid_arguments"),
        ("get_current_time", '{"timezone":"Invalid/Zone"}', "tool_failed"),
    ],
)
async def test_tool_rejection_and_schema(name, arguments, error):
    result = await ToolExecutor(default_registry(), 0.1).execute(ToolCall("t", name, arguments))
    assert result == {"ok": False, "error": error}


async def test_tool_timeout_cancels_handler_and_model_gets_error():
    cancelled = asyncio.Event()

    async def slow():
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    registry = ToolRegistry()
    registry.register(Tool("slow", "slow", ToolArguments, slow))
    client = ScriptedClient(call("slow"), ModelResponse("工具超时"))
    reply = await service(client, config(tool_timeout_seconds=0.01), registry).handle(msg())
    assert reply.text == "工具超时" and cancelled.is_set()
    assert json.loads(client.requests[1][0][-1]["content"])["error"] == "tool_timeout"
    assert client.requests[1][2] == "none"


async def test_tool_exception_is_sanitized():
    async def broken():
        raise RuntimeError("secret-do-not-show")

    registry = ToolRegistry()
    registry.register(Tool("broken", "test", ToolArguments, broken))
    assert await ToolExecutor(registry, 1).execute(ToolCall("x", "broken", "{}")) == {
        "ok": False,
        "error": "tool_failed",
    }


async def test_loop_and_tool_budgets_stop_noncompliant_model():
    client = ScriptedClient(*[call(cid=str(i)) for i in range(5)])
    reply = await service(client, config(max_agent_steps=3, max_tool_calls=1)).handle(msg())
    assert reply.status == "error" and reply.model_steps == 3 and reply.tool_calls == 1
    assert len(client.requests) == 3


async def test_multiple_calls_cannot_exceed_budget():
    response = ModelResponse(
        tool_calls=[ToolCall(str(i), "get_current_time", "{}") for i in range(4)]
    )
    client = ScriptedClient(response, ModelResponse("已达工具上限"))
    reply = await service(client, config(max_tool_calls=2)).handle(msg())
    assert reply.tool_calls == 2
    results = [m for m in client.requests[1][0] if m["role"] == "tool"]
    assert len(results) == 4
    assert [json.loads(r["content"])["ok"] for r in results] == [True, True, False, False]


async def test_failure_stops_other_calls():
    client = ScriptedClient(
        ModelResponse(
            tool_calls=[ToolCall("a", "unknown", "{}"), ToolCall("b", "get_current_time", "{}")]
        ),
        ModelResponse("失败"),
    )
    reply = await service(client).handle(msg())
    assert reply.tool_calls == 1


async def test_overall_timeout_preserves_previous_history():
    client = ScriptedClient(ModelResponse("first"), ModelResponse("never"))
    svc = service(client, config(agent_timeout_seconds=0.05))
    await svc.handle(msg())
    client.delay = 0.2
    result = await svc.handle(msg("second", "2"))
    assert result.status == "error" and "超时" in result.text
    assert len(svc.sessions[msg().thread_id].turns) == 1


async def test_model_failure_does_not_poison_following_turn():
    client = ScriptedClient(call(), ModelError("服务不可用"), ModelResponse("已恢复"))
    svc = service(client)
    assert (await svc.handle(msg())).status == "error"
    assert (await svc.handle(msg("重试", "2"))).text == "已恢复"
    assert not any(m.get("tool_calls") for m in client.requests[2][0])


async def test_duplicate_concurrent_message_only_runs_once():
    client = ScriptedClient(ModelResponse("ok"), delay=0.01)
    svc = service(client)
    a, b = await asyncio.gather(svc.handle(msg()), svc.handle(msg()))
    assert a == b and len(client.requests) == 1


async def test_concurrent_turns_are_serialized():
    client = ScriptedClient(ModelResponse("first"), ModelResponse("second"), delay=0.01)
    svc = service(client)
    await asyncio.gather(svc.handle(msg()), svc.handle(msg("second", "2")))
    assert len(client.requests[1][0]) == 4


@pytest.mark.parametrize("second", [msg(user="b"), msg(chat="other"), msg(platform="qq")])
async def test_identity_and_chat_isolation(second):
    client = ScriptedClient(ModelResponse("private-one"), ModelResponse("two"))
    svc = service(client)
    await svc.handle(msg())
    await svc.handle(second)
    assert len(client.requests[1][0]) == 2


async def test_reset_and_history_bounds():
    client = ScriptedClient(*[ModelResponse("ok") for _ in range(22)])
    svc = service(client)
    for i in range(21):
        await svc.handle(msg(mid=str(i)))
    assert len(svc.sessions[msg().thread_id].turns) == 20
    await svc.reset(msg())
    await svc.handle(msg(mid="new"))
    assert len(client.requests[-1][0]) == 2


def test_config_missing_credentials_and_secret_repr():
    settings = config(deepseek_api_key="secret-do-not-show")
    assert "secret-do-not-show" not in repr(settings)
    with pytest.raises(ConfigurationError, match="DEEPSEEK_BASE_URL"):
        settings.require_live()


def test_input_validation_and_thread_delimiter_collision():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        msg(" ")
    assert msg(chat="a:b", user="c").thread_id != msg(chat="a", user="b:c").thread_id


def test_registry_rejects_duplicate_sync_and_side_effect_tools():
    registry = default_registry()
    with pytest.raises(ValueError, match="重复"):
        registry.register(registry.get("get_current_time"))
    with pytest.raises(ValueError, match="async"):
        registry.register(Tool("sync", "test", ToolArguments, dict))

    async def effect():
        return {}

    with pytest.raises(ValueError, match="副作用"):
        registry.register(Tool("effect", "test", ToolArguments, effect, side_effect=True))
