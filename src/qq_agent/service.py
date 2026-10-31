import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from uuid import uuid4

from langgraph.types import Overwrite

from qq_agent.config import Settings
from qq_agent.graph import build_graph
from qq_agent.memory import MemoryManager, memory_context
from qq_agent.messages import IncomingMessage
from qq_agent.model import LLMClient, is_interruption_marker
from qq_agent.observability import request_context
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
            memory_context.reset(memory_token)
            request_context.reset(token)

    async def _handle(self, message):
        started = time.monotonic()
        session = self.sessions.setdefault(message.thread_id, Session())
        async with session.lock:
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
