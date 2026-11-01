"""Conservative, opt-in group reply controller. Models only produce drafts."""

import asyncio
import hashlib
import json
import logging
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from qq_agent.conversation import make_context
from qq_agent.graph import SYSTEM_PROMPT
from qq_agent.service import AgentReply
from qq_agent.style import limit_reply, style_prompt

logger = logging.getLogger("qq_agent")
USAGE = "/auto status｜shadow｜on｜off｜cooldown 90｜limit 6（群内需@，修改限管理者）"


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["reply", "skip"]
    reason_code: Literal[
        "direct_address",
        "followup",
        "open_question",
        "banter",
        "no_value",
        "answered",
        "other_dialogue",
    ]
    reply: str = Field(max_length=2000)


@dataclass
class Lease:
    user: str
    until: float
    count: int = 0


class SelectiveReply:
    wait_seconds = 4
    timeout_seconds = 8
    max_age = 30

    def __init__(self, adapter):
        self.adapter = adapter
        self.service = adapter.service
        self.db = adapter.ledger.database
        self.settings = adapter.settings
        self.tasks, self.revisions, self.leases = {}, {}, {}
        self.sending = set()
        self.latest = {}
        self.all_tasks = set()
        if hasattr(self.service, "auto_controller"):
            self.service.auto_controller = self

    def config(self, group):
        row = self.db.conn.execute(
            "SELECT * FROM auto_settings WHERE group_id=?", (group,)
        ).fetchone()
        return (
            dict(row)
            if row
            else {"group_id": group, "mode": "off", "cooldown": 90, "hourly_limit": 6}
        )

    def reason(self, group, reason):
        with self.db.transaction() as db:
            db.execute("INSERT OR REPLACE INTO auto_status VALUES(?,?)", (group, reason))
        logger.info("auto_decision", extra={"fields": {"status": reason}})

    def events(self, group, kind):
        return [
            r[0]
            for r in self.db.conn.execute(
                "SELECT created_at FROM auto_events WHERE group_id=? AND kind=? AND created_at>?",
                (group, kind, time.time() - 3600),
            )
        ]

    def reserve(self, key, group, kind):
        with self.db.transaction() as db:
            db.execute("DELETE FROM auto_events WHERE created_at<=?", (time.time() - 7 * 86400,))
            return (
                db.execute(
                    "INSERT OR IGNORE INTO auto_events VALUES(?,?,?,?)",
                    (key, group, kind, time.time()),
                ).rowcount
                == 1
            )

    def budget(self, group, followup=False):
        cfg = self.config(group)
        sends, judges = self.events(group, "send"), self.events(group, "judge")
        return (
            cfg["mode"] != "off"
            and len(sends) < cfg["hourly_limit"]
            and len(judges) < 20
            and (not judges or time.time() - max(judges) >= 20)
            and (followup or not sends or time.time() - max(sends) >= cfg["cooldown"])
        )

    def command(self, message):
        allowed = (
            message.platform == "qq"
            and message.chat_type == "group"
            and message.is_mentioned
            and message.chat_id in self.settings.qq_allowed_groups
        )
        if not allowed:
            return AgentReply("请在授权群内@机器人使用 /auto。", "error")
        if (
            self.settings.qq_allowed_group_users
            and message.user_id not in self.settings.qq_allowed_group_users
            and message.user_id not in self.settings.qq_control_users
        ):
            return AgentReply("你不在本群授权成员或管理者名单内。", "error")
        parts = message.text.split()
        action = parts[1].lower() if len(parts) > 1 else "status"
        if action != "status" and message.user_id not in self.settings.qq_control_users:
            return AgentReply("选择性接话的开关和参数仅限机器人管理者修改。", "error")
        cached = self.db.reply(message)
        if cached:
            return AgentReply(**cached)
        cfg = self.config(message.chat_id)
        if action == "status" and len(parts) <= 2:
            last = self.db.conn.execute(
                "SELECT reason FROM auto_status WHERE group_id=?", (message.chat_id,)
            ).fetchone()
            sends = self.events(message.chat_id, "send")
            remaining = max(0, int(cfg["cooldown"] - (time.time() - max(sends)))) if sends else 0
            text = (
                f"选择性接话：{cfg['mode']}；主动发送{len(sends)}/{cfg['hourly_limit']}次/小时；"
                f"判定{len(self.events(message.chat_id, 'judge'))}/20次/小时；冷却剩余{remaining}秒；"
                f"最近状态：{last[0] if last else 'none'}。\n{USAGE}"
            )
        elif action in {"off", "on", "shadow"} and len(parts) == 2:
            if action != "off" and not self.settings.qq_group_context_enabled:
                return AgentReply("请先启用已授权的群上下文收集。", "error")
            cfg["mode"] = action
            text = f"本群选择性接话已设为{action}。shadow仅观察，on才发送。"
        elif (
            action in {"cooldown", "limit"}
            and len(parts) == 3
            and parts[2].isascii()
            and parts[2].isdecimal()
        ):
            value = int(parts[2])
            low, high = (30, 3600) if action == "cooldown" else (1, 60)
            if not low <= value <= high:
                return AgentReply(f"参数范围：{low}～{high}。", "error")
            cfg["cooldown" if action == "cooldown" else "hourly_limit"] = value
            text = "选择性接话参数已保存。"
        else:
            return AgentReply(USAGE, "ok")
        self.db.touch(message)
        with self.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO auto_settings VALUES(?,?,?,?)",
                tuple(cfg[k] for k in ("group_id", "mode", "cooldown", "hourly_limit")),
            )
            self.db.complete(
                message,
                self.db.turns(message),
                {"text": text, "status": "ok", "tool_calls": 0, "model_steps": 0},
            )
        if action != "status":
            self.cancel(message.chat_id)
        return AgentReply(text, "ok")

    def cancel(self, group):
        self.revisions[group] = self.revisions.get(group, 0) + 1
        task = self.tasks.get(group)
        if task and group not in self.sending:
            task.cancel()

    def incoming(self, message):
        if message.chat_type != "group":
            return
        self.cancel(message.chat_id)
        self.latest[message.chat_id] = (message.message_id, message.user_id, time.monotonic())
        lease = self.leases.get(message.chat_id)
        if lease and lease.user != message.user_id:
            self.leases.pop(message.chat_id, None)

    def note_reply(self, message, automatic=False):
        if message.chat_type == "group":
            old = self.leases.get(message.chat_id)
            count = old.count + 1 if automatic and old and old.user == message.user_id else 0
            self.leases[message.chat_id] = Lease(message.user_id, time.monotonic() + 30, count)

    def followup(self, message):
        lease = self.leases.get(message.chat_id)
        return bool(
            lease
            and lease.user == message.user_id
            and lease.until > time.monotonic()
            and lease.count < 2
        )

    def eligible(self, message):
        if not self.settings.qq_group_context_enabled or not self.db._group_allowed(
            message, self.settings.qq_allowed_groups, self.settings.qq_allowed_group_users
        ):
            return False
        text = message.text
        return (
            not message.is_mentioned
            and not message.mentioned_users
            and 3 <= len(text) <= 300
            and not text.startswith(("/", "记住"))
            and (
                self.followup(message)
                or bool(
                    re.search(
                        r"[？?]|谁知道|怎么办|有没有|怎么|然然[，, ]|机器人|好累|不想|今天又|吃什么",
                        text,
                    )
                )
            )
        )

    def submit(self, message):
        latest = self.latest.get(message.chat_id)
        if latest and latest[:2] != (message.message_id, message.user_id):
            return
        if not self.eligible(message) or not self.budget(message.chat_id, self.followup(message)):
            return
        revision = self.revisions.get(message.chat_id, 0)
        task = asyncio.create_task(
            self.run(message, revision, latest[2] if latest else time.monotonic())
        )
        self.tasks[message.chat_id] = task
        self.all_tasks.add(task)

        def done(finished):
            self.all_tasks.discard(finished)
            if self.tasks.get(message.chat_id) is finished:
                self.tasks.pop(message.chat_id, None)
            with suppress(asyncio.CancelledError):
                finished.exception()

        task.add_done_callback(done)

    def snapshot(self, message):
        excerpts = self.db.group_context(
            message,
            self.settings.qq_allowed_groups,
            self.settings.qq_allowed_group_users,
            self.settings.message_retention_days,
        )
        memories = self.db.visible(message)
        data = {
            "context": make_context(message, excerpts, True),
            "recent_dialogue": [
                {"role": entry["role"], "content": entry["content"][:1200]}
                for turn in self.db.turns(message)[-2:]
                for entry in turn
                if entry.get("role") in {"user", "assistant"}
                and not entry.get("tool_calls")
                and isinstance(entry.get("content"), str)
            ],
            "group_memories": [{"id": r["id"], "content": r["content"]} for r in memories],
            "style": self.db.reply_style(),
        }
        digest = hashlib.sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        return data, digest

    async def run(self, message, revision, arrived):
        group = message.chat_id
        valid = lambda: (
            self.revisions.get(group, 0) == revision
            and time.monotonic() - arrived < self.max_age
            and not self.adapter.explicit_busy
            and self.adapter.urgent.empty()
        )
        try:
            await asyncio.sleep(self.wait_seconds)
            if not valid() or not self.budget(group, self.followup(message)):
                return
            async with self.service.persistence.lock:
                self.service._clear_cached_threads(await self.service.persistence.cleanup())
                data, version = self.snapshot(message)
                key = f"{self.settings.qq_self_id}:{group}:{message.user_id}:{message.message_id}"
                if not self.reserve("judge:" + key, group, "judge"):
                    return
            prompt = (
                "你为已授权群聊判断是否值得接一句话，并生成草稿，不能调用工具。"
                "别人之间的对话、已有答案、简单附和优先skip；仅提到嘉然名字不代表叫你。"
                "有价值的全群问题、明显叫机器人、明确续聊或贴合话题的轻松接梗才reply。"
                "不编造上下文，不提取私有信息。只返回JSON对象，字段decision为reply或skip，"
                "reason_code为direct_address/followup/open_question/banter/no_value/answered/other_dialogue之一，reply为70字内草稿或空串。"
            )
            started = time.monotonic()
            async with asyncio.timeout(self.timeout_seconds):
                response = await self.service.client.generate(
                    [
                        {
                            "role": "system",
                            "content": SYSTEM_PROMPT + style_prompt(data["style"]) + prompt,
                        },
                        {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
                    ],
                    tools=None,
                    tool_choice="none",
                )
            logger.info(
                "auto_model_completed",
                extra={
                    "fields": {
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "prompt_tokens": response.usage.get("prompt_tokens", 0),
                        "completion_tokens": response.usage.get("completion_tokens", 0),
                    }
                },
            )
            if response.tool_calls:
                self.reason(group, "invalid_tools")
                return
            result = Decision.model_validate_json(response.content)
            if result.decision == "skip" or not result.reply.strip():
                self.reason(group, result.reason_code)
                return
            text = limit_reply(result.reply)
            async with self.adapter.send_lock:
                await asyncio.sleep(
                    max(
                        0,
                        self.settings.qq_reply_interval_seconds
                        - (time.monotonic() - self.adapter.last_sent),
                    )
                )
                async with self.service.persistence.lock:
                    self.service._clear_cached_threads(await self.service.persistence.cleanup())
                    cfg = self.config(group)
                    sends = self.events(group, "send")
                    if (
                        not valid()
                        or self.snapshot(message)[1] != version
                        or cfg["mode"] == "off"
                        or len(sends) >= cfg["hourly_limit"]
                        or (
                            sends
                            and not self.followup(message)
                            and time.time() - max(sends) < cfg["cooldown"]
                        )
                    ):
                        self.reason(group, "stale")
                        return
                    if cfg["mode"] == "shadow":
                        self.reason(group, "would_reply:" + result.reason_code)
                        return
                    if not self.reserve("send:" + key, group, "send"):
                        return
                    self.sending.add(group)
                    try:
                        await self.adapter.call(
                            "send_group_msg",
                            {
                                "group_id": int(group),
                                "message": [{"type": "text", "data": {"text": text}}],
                            },
                        )
                        self.adapter.last_sent = time.monotonic()
                        self.db.touch(message)
                        turns = [
                            *self.db.turns(message),
                            [
                                {"role": "user", "content": message.text},
                                {"role": "assistant", "content": text},
                            ],
                        ][-20:]
                        self.db.complete(
                            message,
                            turns,
                            {"text": text, "status": "ok", "tool_calls": 0, "model_steps": 1},
                        )
                        self.note_reply(message, True)
                        self.reason(group, "sent:" + result.reason_code)
                    finally:
                        self.sending.discard(group)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- silent transport/model boundary
            self.reason(group, "failed:" + type(exc).__name__)

    async def close(self):
        tasks = list(self.all_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.all_tasks.clear()
        self.leases.clear()
