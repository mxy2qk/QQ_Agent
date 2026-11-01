import argparse
import asyncio
import sys
from contextlib import AsyncExitStack, suppress
from uuid import uuid4

from pydantic import ValidationError

from qq_agent.config import ConfigurationError, Settings
from qq_agent.messages import IncomingMessage
from qq_agent.model import DeepSeekClient, DemoClient, ModelError
from qq_agent.observability import setup_logging
from qq_agent.persistence import open_persistence
from qq_agent.public_sources import PublicSources
from qq_agent.service import AgentService
from qq_agent.skills import SkillManager
from qq_agent.tools import default_registry


async def verify_live(client):
    """Verify a real text completion without registering a synthetic query tool."""
    greeting = await client.generate([{"role": "user", "content": "请简短问好"}])
    if not greeting.content or greeting.tool_calls:
        raise ModelError("普通文本验证失败")
    print("真实 API 验证通过：普通文本请求。")


async def run(args, log_handler=None):
    settings = Settings()
    setup_logging(settings.log_level)
    if log_handler is not None:
        import logging

        logging.getLogger("qq_agent").addHandler(log_handler)
    client = (
        DemoClient()
        if args.demo or args.list_skills or args.verify_sources
        else DeepSeekClient(settings)
    )
    resources = AsyncExitStack()
    cleanup_task = None
    try:
        registry = default_registry()
        if args.verify_api:
            async with asyncio.timeout(settings.agent_timeout_seconds):
                await verify_live(client)
            return 0
        sources = PublicSources(settings)
        resources.push_async_callback(sources.close)
        sources.register(registry)
        manager = SkillManager(registry)
        if args.verify_sources:
            import json

            from qq_agent.verification import verify_sources

            async with asyncio.timeout(settings.agent_timeout_seconds):
                report = await verify_sources(registry, settings)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["complete"] else 1
        if args.list_skills:
            print(manager.catalog())
            return 0
        if args.qq:
            settings.require_qq()
            if (
                args.demo
                or args.ephemeral
                or args.message
                or args.group
                or args.user != "local-user"
            ):
                raise ConfigurationError(
                    "QQ 模式必须使用持久化和可信事件身份，不能混用模拟/单条消息参数"
                )
        if args.data_dir:
            from pathlib import Path

            settings.database_path = str(Path(args.data_dir) / "agent.db")
            settings.checkpoint_db_path = str(Path(args.data_dir) / "checkpoints.db")
        elif args.demo:
            settings.database_path = "data/demo/agent.db"
            settings.checkpoint_db_path = "data/demo/checkpoints.db"
        persistence = (
            None
            if args.ephemeral
            else await resources.enter_async_context(open_persistence(settings))
        )
        service = AgentService(client, registry, settings, persistence, manager)
        service.image_source = sources.pixiv

        async def periodic_cleanup():
            while True:
                await asyncio.sleep(60)
                await service.cleanup()

        if persistence:
            cleanup_task = asyncio.create_task(periodic_cleanup())

        if args.qq:
            from qq_agent.channels.onebot import OneBotAdapter

            await OneBotAdapter(service, settings, persistence.database).run()
            return 0

        def message(text):
            return IncomingMessage(
                platform="cli",
                message_id=str(uuid4()),
                chat_type="group" if args.group else "private",
                chat_id=args.group or args.session,
                user_id=args.user,
                text=text,
            )

        if args.message:
            reply = await service.handle(message(args.message))
            print(reply.text)
            return 0 if reply.status == "ok" else 1
        print("QQ Personal Agent" + (" [离线演示]" if args.demo else " [DeepSeek]"))
        print("输入 /exit 退出，/reset 清空近期会话，/help 查看操作帮助。")
        print(
            "会话仅在当前进程保留。"
            if args.ephemeral
            else f"会话持久化，保留 {settings.message_retention_days} 天。"
        )
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
        if cleanup_task:
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task
        await resources.aclose()
        await client.close()


def main():
    # PowerShell pipes use UTF-8; Windows Python may otherwise select a legacy code page.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="DeepSeek + LangGraph CLI Agent")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="无需密钥的离线演示，不调用模型")
    mode.add_argument("--verify-api", action="store_true", help="调用真实 API 验证普通文本请求")
    mode.add_argument(
        "--verify-sources", action="store_true", help="验证真实数据源，不调用模型或打开聊天数据库"
    )
    parser.add_argument("--message", help="单条消息模式")
    mode.add_argument("--qq", action="store_true", help="连接已配置的 NapCat OneBot 正向 WebSocket")
    parser.add_argument("--list-skills", action="store_true", help="列出已安装技能，不调用模型")
    parser.add_argument("--session", default="local")
    parser.add_argument("--user", default="local-user")
    parser.add_argument("--group", help="模拟群 ID；仅本地测试身份隔离")
    parser.add_argument("--data-dir", help="独立测试数据目录，不覆盖默认数据库")
    parser.add_argument(
        "--ephemeral", action="store_true", help="仅内存运行，不启用长期记忆或持久化"
    )
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
