import json

from qq_agent.config import Settings
from qq_agent.messages import IncomingMessage
from qq_agent.model import ModelResponse
from qq_agent.service import AgentService, Session
from qq_agent.tools import default_registry


def test_no_builtin_time_query_tool():
    registry = default_registry()
    assert registry.schemas() == []
    assert registry.get("get_current_time") is None


async def test_qq_time_question_has_no_shortcut_and_uses_normal_model():
    requests = []

    class Client:
        async def generate(self, messages, tools=None, tool_choice="auto"):
            requests.append((messages, tools))
            return ModelResponse("我无法核实当前北京时间，因为没有可用的实时查询工具。")

    svc = AgentService(Client(), default_registry(), Settings(_env_file=None))
    message = IncomingMessage(
        platform="qq",
        user_id="200",
        chat_type="private",
        chat_id="200",
        message_id="new",
        text="现在的北京时间",
    )
    reply = await svc.handle(message)
    assert reply.model_steps == 1 and reply.tool_calls == 0
    assert "无法核实" in reply.text
    assert len(requests) == 1 and requests[0][1] == []
    assert "不得" in requests[0][0][0]["content"]
    assert "time" not in svc.graph.nodes


async def test_removed_tool_history_is_not_fed_into_future_requests():
    requests = []

    class Client:
        async def generate(self, messages, tools=None, tool_choice="auto"):
            requests.append(messages)
            return ModelResponse("你好")

    svc = AgentService(Client(), default_registry(), Settings(_env_file=None))
    message = IncomingMessage(
        platform="qq",
        user_id="200",
        chat_type="private",
        chat_id="200",
        message_id="new",
        text="你好",
    )
    svc.sessions[message.thread_id] = Session(
        turns=[
            [
                {"role": "user", "content": "过去的查询"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "old",
                            "type": "function",
                            "function": {"name": "retired_tool", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "old", "content": "retired-data"},
                {"role": "assistant", "content": "retired-answer"},
            ]
        ]
    )
    reply = await svc.handle(message)
    assert reply.text == "你好"
    assert "retired-" not in json.dumps(requests, ensure_ascii=False)
