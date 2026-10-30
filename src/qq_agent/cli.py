import argparse
import asyncio
import sys
from uuid import uuid4

from pydantic import ValidationError

from qq_agent.config import ConfigurationError, Settings
from qq_agent.messages import IncomingMessage
from qq_agent.model import DeepSeekClient, DemoClient, ModelError
from qq_agent.observability import setup_logging
from qq_agent.service import AgentService
from qq_agent.tools import ToolExecutor, default_registry


async def verify_live(client, registry, settings):
    """Verify plain completion, forced tool call, real execution and result round trip."""
    greeting = await client.generate([{"role": "user", "content": "请简短问好"}])
    if not greeting.content or greeting.tool_calls:
        raise ModelError("普通文本验证失败")
    messages = [{"role": "user", "content": "查询上海的当前时间，必须使用时间工具"}]
    response = await client.generate(
        messages,
        registry.schemas(),
        tool_choice={"type": "function", "function": {"name": "get_current_time"}},
    )
    if len(response.tool_calls) != 1 or response.tool_calls[0].name != "get_current_time":
        raise ModelError("Tool Calling 验证失败：未返回预期的时间工具调用")
    import json

    call = response.tool_calls[0]
    result = await ToolExecutor(registry, settings.tool_timeout_seconds).execute(call)
    if not result["ok"]:
        raise ModelError("Tool Calling 参数或执行验证失败")
    messages.extend(
        [
            response.as_message(),
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result, ensure_ascii=False),
            },
        ]
    )
    final = await client.generate(messages, registry.schemas(), tool_choice="none")
    if not final.content or final.tool_calls:
        raise ModelError("工具结果回传验证失败")
    print("真实 API 验证通过：普通文本、Tool Calling、参数校验、执行、结果回传。")


async def run(args):
    settings = Settings()
    setup_logging(settings.log_level)
    client = DemoClient() if args.demo else DeepSeekClient(settings)
    try:
        registry = default_registry(settings.timezone)
        if args.verify_api:
            async with asyncio.timeout(settings.agent_timeout_seconds):
                await verify_live(client, registry, settings)
            return 0
        service = AgentService(client, registry, settings)

        def message(text):
            return IncomingMessage(
                platform="cli",
                message_id=str(uuid4()),
                chat_type="private",
                chat_id=args.session,
                user_id=args.user,
                text=text,
            )

        if args.message:
            reply = await service.handle(message(args.message))
            print(reply.text)
            return 0 if reply.status == "ok" else 1
        print("QQ Personal Agent" + (" [离线演示]" if args.demo else " [DeepSeek]"))
        print("输入 /exit 退出，/reset 清空本次会话。会话只保留在当前进程。")
        while True:
            try:
                text = await asyncio.to_thread(input, "你> ")
            except EOFError:
                break
            text = text.strip()
            if text in {"/exit", "/quit"}:
                break
            if not text:
                continue
            if text == "/reset":
                await service.reset(message("reset"))
                print("已清空当前会话。")
                continue
            if len(text) > 32000:
                print("消息过长，请控制在 32000 字符以内。")
                continue
            print("助手> " + (await service.handle(message(text))).text)
        return 0
    finally:
        await client.close()


def main():
    # PowerShell pipes use UTF-8; Windows Python may otherwise select a legacy code page.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="DeepSeek + LangGraph CLI Agent")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="无需密钥的离线演示，不调用模型")
    mode.add_argument("--verify-api", action="store_true", help="调用真实 API 验证文本及工具闭环")
    parser.add_argument("--message", help="单条消息模式")
    parser.add_argument("--session", default="local")
    parser.add_argument("--user", default="local-user")
    args = parser.parse_args()
    try:
        code = asyncio.run(run(args))
    except (ConfigurationError, ModelError) as exc:
        print(str(exc), file=sys.stderr)
        code = 2
    except ValidationError as exc:
        # Pydantic normally includes input values: do not print them.
        fields = ", ".join(".".join(map(str, e["loc"])) for e in exc.errors())
        print("配置或消息字段无效：" + fields, file=sys.stderr)
        code = 2
    except TimeoutError:
        print("API 验证超时", file=sys.stderr)
        code = 2
    except KeyboardInterrupt:
        code = 130
    except Exception as exc:  # noqa: BLE001 -- CLI must not expose raw SDK/config values
        print("启动或 API 验证失败，错误类型：" + type(exc).__name__, file=sys.stderr)
        code = 2
    sys.exit(code)
