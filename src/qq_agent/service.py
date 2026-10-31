import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from uuid import uuid4

from langgraph.types import Overwrite

from qq_agent.command_help import command_help
from qq_agent.config import Settings
from qq_agent.conversation import conversation_context, make_context, member_targets
from qq_agent.graph import build_graph
from qq_agent.memory import MemoryManager, memory_context
from qq_agent.messages import IncomingMessage
from qq_agent.model import LLMClient, is_interruption_marker
from qq_agent.observability import request_context
from qq_agent.style import DEFAULT_STYLE, is_style_command, reply_style_context, style_command
from qq_agent.tools import ToolRegistry

logger = logging.getLogger("qq_agent")


@dataclass(frozen=True)
class AgentReply:
    text: str
    status: str
    tool_calls: int = 0
    model_steps: int = 0


@dataclass
class Session:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turns: list[list[dict]] = field(default_factory=list)
    replies: OrderedDict = field(default_factory=OrderedDict)


class AgentService:
    def __init__(
        self,
        client: LLMClient,
        registry: ToolRegistry,
        settings: Settings,
        persistence=None,
        skill_manager=None,
    ):
        self.settings = settings
        self.registry = registry
        self.skill_manager = skill_manager
        self.persistence = persistence
        self.database = persistence.database if persistence else None
        self.memory = (
            MemoryManager(self.database, settings.memory_extraction_enabled)
            if persistence
            else None
        )
        self.graph = build_graph(
            client,
            registry,
            settings,
            persistence.checkpointer if persistence else None,
            skill_manager,
        )
        self.sessions: dict[str, Session] = {}
        self.ephemeral_style = DEFAULT_STYLE

    async def handle(self, message: IncomingMessage):
        token = request_context.set(
            {
                "request_id": str(uuid4()),
                "platform": message.platform,
                "chat_id": message.chat_id,
                "message_id": message.message_id,
                "thread_id": message.thread_id,
            }
        )
        memory_token = memory_context.set(None)
        conversation_token = conversation_context.set(None)
        style_token = reply_style_context.set(None)
        try:
            async with asyncio.timeout(self.settings.agent_timeout_seconds):
                if self.persistence:
                    async with self.persistence.lock:
                        self._clear_cached_threads(await self.persistence.cleanup())
                        return await self._handle(message)
                return await self._handle(message)
        except TimeoutError:
            logger.warning("request_timeout", extra={"fields": {"status": "error"}})
            return AgentReply("本次请求超时，请稍后重试", "error")
        except Exception as exc:  # noqa: BLE001 -- sanitize the external boundary
            logger.error(
                "request_failed",
                extra={"fields": {"status": "error", "error_type": type(exc).__name__}},
            )
            return AgentReply("本次请求失败，请检查配置或查看日志中的错误类型", "error")
        finally:
            reply_style_context.reset(style_token)
            conversation_context.reset(conversation_token)
            memory_context.reset(memory_token)
            request_context.reset(token)

    async def _handle(self, message):
        started = time.monotonic()
        session = self.sessions.setdefault(message.thread_id, Session())
        async with session.lock:
            help_text = command_help(
                message.text,
                message,
                self.settings,
                self.skill_manager,
                persistent=self.persistence is not None,
            )
            if help_text is not None:
                if self.database:
                    cached = self.database.reply(message)
                    if cached:
                        return AgentReply(**cached)
                    self.database.touch(message)
                    self.database.complete(
                        message, self.database.turns(message), asdict(AgentReply(help_text, "ok"))
                    )
                else:
                    session.replies[message.message_id] = AgentReply(help_text, "ok")
                    if len(session.replies) > 256:
                        session.replies.popitem(last=False)
                return AgentReply(help_text, "ok")
            if is_style_command(message.text):
                return self._handle_style(message, session)
            if self.database:
                cached = self.database.reply(message)
                if cached:
                    logger.info("message_duplicate")
                    return AgentReply(**cached)
                self.database.touch(message)
                if message.text == "/reset":
                    self.database.reset(message)
                    self._clear_cached_threads(await self.persistence.flush_purges())
                    return AgentReply("近期会话已清空，明确保存的长期记忆保留。", "ok")
                session.turns = self.database.turns(message)
                command_reply = self.memory.handle_command(message)
                if command_reply is not None:
                    self._clear_cached_threads(await self.persistence.flush_purges())
                    session.turns = self.database.turns(message)
                    reply = AgentReply(command_reply, "ok")
                    self.database.complete(message, session.turns, asdict(reply))
                    return reply
                rows = self.memory.retrieve(message)
                memory_context.set(rows)
                logger.info("memory_loaded", extra={"fields": {"memory_count": len(rows)}})
            elif message.message_id in session.replies:
                logger.info("message_duplicate")
                return session.replies[message.message_id]
            if message.platform == "qq":
                excerpts = []
                enabled = self.settings.qq_group_context_enabled
                if enabled and self.database and message.chat_type == "group":
                    self.database.observe_group(
                        message,
                        self.settings.qq_allowed_groups,
                        self.settings.qq_allowed_group_users,
                    )
                    excerpts = self.database.group_context(
                        message,
                        self.settings.qq_allowed_groups,
                        self.settings.qq_allowed_group_users,
                        self.settings.message_retention_days,
                        member_targets(message),
                    )
                conversation_context.set(make_context(message, excerpts, enabled))
            # Keep raw messages for their normal retention period, but omit failed
            # turns entirely from model context. Rewriting a failure as an assistant
            # reply still encourages the model to repeat that failed answer.
            session.turns = [
                turn
                for turn in session.turns
                if not any(
                    m.get("role") == "assistant"
                    and not m.get("tool_calls")
                    and is_interruption_marker(m.get("content"))
                    for m in turn
                )
                and not any(
                    not self.registry.get(call.get("function", {}).get("name"))
                    for m in turn
                    if m.get("role") == "assistant"
                    for call in m.get("tool_calls", [])
                )
            ]
            history = [m for turn in session.turns for m in turn]
            current_style = (
                (self.database.reply_style() or DEFAULT_STYLE)
                if self.database
                else self.ephemeral_style
            )
            reply_style_context.set(current_style)
            user_message = {"role": "user", "content": message.text}
            config = {
                "recursion_limit": self.settings.max_agent_steps * 3 + 10,
                "configurable": {"thread_id": message.thread_id},
            }
            state = await self.graph.ainvoke(
                {
                    "messages": Overwrite([*history, user_message]),
                    "platform": message.platform,
                    "chat_type": message.chat_type,
                    "chat_id": message.chat_id,
                    "user_id": message.user_id,
                    "current_message_id": message.message_id,
                },
                config,
            )
            reply = AgentReply(
                state["reply"],
                "error" if state.get("error") else "ok",
                state["tool_count"],
                state["steps"],
            )
            turn = (
                [user_message, {"role": "assistant", "content": reply.text}]
                if state.get("error")
                else state["messages"][len(history) :]
            )
            session.turns = [*session.turns, turn][-20:]
            if self.database:
                # Recovery reads the committed application snapshot; checkpoints
                # contain only completed, compact graph state after successful turns.
                with self.database.transaction() as db:
                    db.execute("INSERT OR IGNORE INTO purge_queue VALUES(?)", (message.thread_id,))
                    self.database.complete(message, session.turns, asdict(reply))
                await self.persistence.flush_purges()
                state["messages"] = Overwrite([m for t in session.turns for m in t])
                await self.graph.aupdate_state(config, state, as_node="finalize")
            else:
                session.replies[message.message_id] = reply
                if len(session.replies) > 256:
                    session.replies.popitem(last=False)
            logger.info(
                "request_completed",
                extra={
                    "fields": {
                        "status": reply.status,
                        "agent_step": reply.model_steps,
                        "duration_ms": round((time.monotonic() - started) * 1000),
                    }
                },
            )
            return reply

    def _handle_style(self, message, session):
        allowed = message.platform == "cli" or (
            message.platform == "qq"
            and (
                (
                    message.chat_type == "group"
                    and message.chat_id in self.settings.qq_allowed_groups
                    and message.is_mentioned
                )
                or (
                    message.chat_type == "private"
                    and self.settings.qq_private_enabled
                    and message.user_id in self.settings.qq_allowed_users
                )
            )
        )
        if not allowed:
            return AgentReply("请在授权群内@机器人使用 /style，或通过授权好友私聊操作。", "error")
        cached = (
            self.database.reply(message)
            if self.database
            else session.replies.get(message.message_id)
        )
        if cached:
            return AgentReply(**cached) if self.database else cached
        current = (
            (self.database.reply_style() or DEFAULT_STYLE)
            if self.database
            else self.ephemeral_style
        )
        result = style_command(message.text, current)
        reply = AgentReply(result.reply, "ok")
        if self.database:
            self.database.touch(message)
            self.database.complete_style(message, result, asdict(reply))
        else:
            if result.reset:
                self.ephemeral_style = DEFAULT_STYLE
            elif result.value is not None:
                self.ephemeral_style = result.value
            session.replies[message.message_id] = reply
            if len(session.replies) > 256:
                session.replies.popitem(last=False)
        return reply

    async def observe_group(self, message):
        if not self.persistence or not self.settings.qq_group_context_enabled:
            return
        async with self.persistence.lock:
            self._clear_cached_threads(await self.persistence.cleanup())
            self.database.observe_group(
                message, self.settings.qq_allowed_groups, self.settings.qq_allowed_group_users
            )

    async def cleanup(self):
        if self.persistence:
            async with self.persistence.lock:
                purged = await self.persistence.cleanup()
                self._clear_cached_threads(purged)

    def _clear_cached_threads(self, thread_ids):
        for thread_id in thread_ids:
            session = self.sessions.get(thread_id)
            if session:
                session.turns.clear()
                session.replies.clear()

    async def reset(self, message: IncomingMessage):
        if self.persistence:
            async with self.persistence.lock:
                self.database.reset(message)
                await self.persistence.flush_purges()
                self.sessions.pop(message.thread_id, None)
        else:
            session = self.sessions.get(message.thread_id)
            if session:
                async with session.lock:
                    session.turns.clear()
                    session.replies.clear()
