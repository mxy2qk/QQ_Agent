import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from uuid import uuid4

from qq_agent.config import Settings
from qq_agent.graph import build_graph
from qq_agent.messages import IncomingMessage
from qq_agent.model import LLMClient
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
    # Retain whole turns so tool call/result pairs are never split by trimming.
    turns: list[list[dict]] = field(default_factory=list)
    replies: OrderedDict = field(default_factory=OrderedDict)


class AgentService:
    def __init__(self, client: LLMClient, registry: ToolRegistry, settings: Settings):
        self.settings = settings
        self.graph = build_graph(client, registry, settings)
        self.sessions: dict[str, Session] = {}

    async def handle(self, message: IncomingMessage):
        session = self.sessions.setdefault(message.thread_id, Session())
        token = request_context.set(
            {
                "request_id": str(uuid4()),
                "platform": message.platform,
                "chat_id": message.chat_id,
                "message_id": message.message_id,
                "thread_id": message.thread_id,
            }
        )
        started = time.monotonic()
        try:
            # Overall deadline includes time waiting behind another request in this thread.
            async with asyncio.timeout(self.settings.agent_timeout_seconds):
                async with session.lock:
                    if message.message_id in session.replies:
                        logger.info("message_duplicate")
                        return session.replies[message.message_id]
                    history = [m for turn in session.turns for m in turn]
                    user_message = {"role": "user", "content": message.text}
                    state = await self.graph.ainvoke(
                        {
                            "messages": [*history, user_message],
                            "platform": message.platform,
                            "chat_type": message.chat_type,
                            "chat_id": message.chat_id,
                            "user_id": message.user_id,
                            "current_message_id": message.message_id,
                        },
                        {"recursion_limit": self.settings.max_agent_steps * 3 + 10},
                    )
                    reply = AgentReply(
                        state["reply"],
                        "error" if state.get("error") else "ok",
                        state["tool_count"],
                        state["steps"],
                    )
                    if state.get("error"):
                        # Do not persist partially completed tool transactions.
                        turn = [user_message, {"role": "assistant", "content": reply.text}]
                    else:
                        turn = state["messages"][len(history) :]
                    session.turns.append(turn)
                    session.turns = session.turns[-20:]
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
        except TimeoutError:
            logger.warning("request_timeout", extra={"fields": {"status": "error"}})
            return AgentReply("本次请求超时，请稍后重试", "error")
        except Exception as exc:  # noqa: BLE001 -- boundary must sanitize SDK/tool failures
            # Never log exception text or traceback: SDK/tool errors can echo secrets or prompts.
            logger.error(
                "request_failed",
                extra={"fields": {"status": "error", "error_type": type(exc).__name__}},
            )
            return AgentReply("本次请求失败，请检查配置或查看日志中的错误类型", "error")
        finally:
            request_context.reset(token)

    async def reset(self, message: IncomingMessage):
        session = self.sessions.get(message.thread_id)
        if session:
            async with session.lock:
                session.turns.clear()
                session.replies.clear()
