import httpx
import pytest

from qq_agent.config import Settings
from qq_agent.model import DeepSeekClient, ModelError


def client_for(handler):
    from openai import AsyncOpenAI

    client = DeepSeekClient(
        Settings(
            _env_file=None,
            deepseek_api_key="test-secret",
            deepseek_base_url="https://test.invalid",
            deepseek_model="test",
        )
    )
    client.client = AsyncOpenAI(
        api_key="test-secret",
        base_url="https://test.invalid",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return client


async def test_sdk_serialization_and_response_normalization():
    import json

    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "test" and body["thinking"]["type"] == "disabled"
        return httpx.Response(
            200,
            json={
                "id": "r",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "t",
                                    "type": "function",
                                    "function": {"name": "test_echo", "arguments": "{}"},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    client = client_for(handler)
    try:
        result = await client.generate([{"role": "user", "content": "time"}])
        assert result.tool_calls[0].name == "test_echo"
        assert result.usage["total_tokens"] == 15
        assert result.as_message()["tool_calls"][0]["id"] == "t"
    finally:
        await client.close()


@pytest.mark.parametrize("status", [401, 402, 429, 500])
async def test_sdk_errors_do_not_echo_body(status):
    client = client_for(
        lambda request: httpx.Response(
            status, json={"error": {"message": "test-secret private-message", "type": "test"}}
        )
    )
    try:
        with pytest.raises(ModelError) as error:
            await client.generate([{"role": "user", "content": "private-message"}])
        assert "test-secret" not in str(error.value)
        assert "private-message" not in str(error.value)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "message,finish",
    [
        ({"role": "assistant", "content": ""}, "stop"),
        ({"role": "assistant", "content": "partial"}, "length"),
        ({"role": "assistant", "content": None, "refusal": "private-reason"}, "stop"),
    ],
)
async def test_empty_truncated_and_refused_results(message, finish):
    client = client_for(
        lambda request: httpx.Response(
            200,
            json={
                "id": "r",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            },
        )
    )
    try:
        with pytest.raises(ModelError):
            await client.generate([{"role": "user", "content": "hello"}])
    finally:
        await client.close()


async def test_sdk_network_timeout_is_sanitized():
    def handler(request):
        raise httpx.ReadTimeout("test-secret", request=request)

    client = client_for(handler)
    try:
        with pytest.raises(ModelError, match="超时") as error:
            await client.generate([{"role": "user", "content": "hi"}])
        assert "test-secret" not in str(error.value)
    finally:
        await client.close()


@pytest.mark.parametrize("content", ["[用户中断]", "  【用户中断】  ", "[User interrupted]"])
async def test_provider_interruption_placeholder_is_an_error(content):
    from qq_agent.model import ModelInterruptedError

    client = client_for(
        lambda request: httpx.Response(
            200,
            json={
                "id": "r",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ],
            },
        )
    )
    try:
        with pytest.raises(ModelInterruptedError):
            await client.generate([{"role": "user", "content": "现在几点"}])
    finally:
        await client.close()
