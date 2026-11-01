"""OneBot 11 forward WS. The model cannot choose send actions or recipients."""

import asyncio
import base64
import json
import logging
import time
from contextlib import suppress
from uuid import uuid4

from pydantic import ValidationError
from websockets.asyncio.client import connect

from qq_agent.messages import GroupMember, IncomingMessage
from qq_agent.style import is_style_command

logger = logging.getLogger("qq_agent")


class OneBotError(RuntimeError):
    pass


def stable_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TypeError("invalid id")
    value = str(value)
    if not value.isdecimal() or int(value) <= 0 or len(value) > 20:
        raise ValueError("invalid id")
    return str(int(value))


class DeliveryLedger:
    def __init__(self, database, days):
        self.database, self.days = database, days
        self.database.conn.execute(
            "CREATE TABLE IF NOT EXISTS qq_deliveries "
            "(event_key TEXT PRIMARY KEY,status TEXT NOT NULL,updated_at REAL NOT NULL)"
        )
        self.database.conn.commit()

    def reserve(self, key):
        with self.database.transaction() as db:
            db.execute(
                "DELETE FROM qq_deliveries WHERE updated_at<?", (time.time() - self.days * 86400,)
            )
            return (
                db.execute(
                    "INSERT OR IGNORE INTO qq_deliveries VALUES(?,?,?)",
                    (key, "processing", time.time()),
                ).rowcount
                == 1
            )

    def mark(self, key, status):
        with self.database.transaction() as db:
            db.execute(
                "UPDATE qq_deliveries SET status=?,updated_at=? WHERE event_key=?",
                (status, time.time(), key),
            )


class OneBotAdapter:
    def __init__(self, service, settings, database):
        self.service, self.settings = service, settings
        self.ledger = DeliveryLedger(database, settings.message_retention_days)
        self.pending = {}
        self.queue = asyncio.Queue(maxsize=64)
        self.urgent = asyncio.Queue(maxsize=64)
        self.ready = asyncio.Event()
        self.explicit_busy = False
        self.send_lock = asyncio.Lock()
        self.last_reply = {}
        self.last_sent = 0
        self.ws = None
        self.group_members = {}
        self.roster_checked = {}
        from qq_agent.selective_reply import SelectiveReply

        self.auto = SelectiveReply(self)

    def member_snapshot(self, group):
        known = self.group_members.get(group, {})
        ids = self.settings.qq_allowed_group_users or sorted(known)
        return tuple(known.get(uid, GroupMember(user_id=uid)) for uid in ids[:100])

    async def enrich_members(self, message):
        group = message.chat_id
        if time.monotonic() - self.roster_checked.get(group, -600) >= 300:
            self.roster_checked[group] = time.monotonic()
            try:
                async with asyncio.timeout(5):
                    rows = await self.call("get_group_member_list", {"group_id": int(group)})
                if not isinstance(rows, list):
                    raise OneBotError("invalid_member_list")
                known = self.group_members.setdefault(group, {})
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    try:
                        uid = stable_id(row.get("user_id"))
                        row_group = (
                            stable_id(row["group_id"]) if row.get("group_id") is not None else group
                        )
                    except (TypeError, ValueError):
                        continue
                    if row_group != group:
                        continue
                    if uid == self.settings.qq_self_id or (
                        self.settings.qq_allowed_group_users
                        and uid not in self.settings.qq_allowed_group_users
                    ):
                        continue
                    known[uid] = GroupMember(
                        user_id=uid,
                        nickname=row.get("nickname", "")[:256]
                        if isinstance(row.get("nickname"), str)
                        else "",
                        card=row.get("card", "")[:256] if isinstance(row.get("card"), str) else "",
                    )
            except (OneBotError, TimeoutError, ValueError, TypeError):
                logger.warning("qq_member_lookup_failed", extra={"fields": {"status": "fallback"}})
        return message.model_copy(update={"group_members": self.member_snapshot(group)})

    def normalize(self, event):
        try:
            if not isinstance(event, dict) or event.get("post_type") != "message":
                return None
            if stable_id(event.get("self_id")) != self.settings.qq_self_id:
                return None
            user = stable_id(event.get("user_id"))
            if user == self.settings.qq_self_id or event.get("anonymous") is not None:
                return None
            sender = event.get("sender") or {}
            if not isinstance(sender, dict) or (
                sender.get("user_id") is not None and stable_id(sender["user_id"]) != user
            ):
                return None
            kind = event.get("message_type")
            if kind == "group":
                chat = stable_id(event.get("group_id"))
                if chat not in self.settings.qq_allowed_groups:
                    return None
            elif kind == "private" and self.settings.qq_private_enabled:
                if event.get("sub_type") != "friend" or user not in self.settings.qq_allowed_users:
                    return None
                chat = user
            else:
                return None
            # Array segments only: raw CQ strings cannot impersonate mentions.
            segments = event.get("message")
            if not isinstance(segments, list) or len(segments) > 100:
                return None
            pieces, mentioned, mentions = [], False, []
            for segment in segments:
                if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
                    return None
                if segment.get("type") == "at":
                    target = str(segment["data"].get("qq"))
                    mentioned |= target == self.settings.qq_self_id
                    if target != self.settings.qq_self_id and target.isdecimal():
                        target = stable_id(target)
                        mentions.append(target)
                        pieces.append(f" @QQ({target}) ")
                elif segment.get("type") == "text":
                    value = segment["data"].get("text")
                    if not isinstance(value, str):
                        return None
                    pieces.append(value)
                # No file, reply, image, anonymous or forwarded-message fetching.
            if kind == "group" and not mentioned and not self.settings.qq_group_context_enabled:
                return None
            content = "".join(pieces).strip()
            if (
                kind == "group"
                and self.settings.qq_allowed_group_users
                and user not in self.settings.qq_allowed_group_users
                and not (
                    mentioned
                    and (
                        is_style_command(content)
                        or (
                            content.split()
                            and content.split()[0].lower() == "/auto"
                            and user in self.settings.qq_control_users
                        )
                    )
                )
            ):
                return None
            if kind == "group" and mentioned and not content:
                if not all(segment.get("type") in {"text", "at"} for segment in segments):
                    return None
                content = "/help"
            message_id = event.get("message_id")
            if isinstance(message_id, bool) or not isinstance(message_id, (int, str)):
                return None
            if kind == "group":
                known = self.group_members.setdefault(chat, {})
                known[user] = GroupMember(
                    user_id=user,
                    nickname=sender.get("nickname", "")[:256]
                    if isinstance(sender.get("nickname"), str)
                    else "",
                    card=sender.get("card", "")[:256]
                    if isinstance(sender.get("card"), str)
                    else "",
                )
            return IncomingMessage(
                platform="qq",
                message_id=str(message_id),
                chat_type=kind,
                chat_id=chat,
                user_id=user,
                nickname=sender.get("card") or sender.get("nickname"),
                text=content,
                is_mentioned=mentioned,
                public_response=(kind == "group"),
                group_members=self.member_snapshot(chat) if kind == "group" else (),
                mentioned_users=tuple(mentions),
            )
        except (ValueError, TypeError, ValidationError):
            return None

    async def call(self, action, params):
        if action not in {
            "get_login_info",
            "get_group_member_list",
            "send_group_msg",
            "send_private_msg",
        }:
            raise OneBotError("action_not_allowed")
        if action == "get_group_member_list":
            try:
                group = stable_id(params.get("group_id"))
            except (TypeError, ValueError):
                raise OneBotError("group_not_allowed") from None
            if group not in self.settings.qq_allowed_groups or set(params) != {"group_id"}:
                raise OneBotError("group_not_allowed")
        if self.ws is None:
            raise OneBotError("disconnected")
        echo = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            await self.ws.send(json.dumps({"action": action, "params": params, "echo": echo}))
            async with asyncio.timeout(self.settings.qq_action_timeout_seconds):
                result = await future
            if result.get("status") != "ok" or result.get("retcode") != 0:
                raise OneBotError("action_failed")
            data = result.get("data")
            if action.startswith("send_") and (
                not isinstance(data, dict) or not isinstance(data.get("message_id"), (str, int))
            ):
                raise OneBotError("invalid_send_ack_delivery_unknown")
            return data
        except TimeoutError:
            raise OneBotError("action_timeout_delivery_unknown") from None
        finally:
            self.pending.pop(echo, None)

    async def receive(self, ws):
        try:
            async for raw in ws:
                try:
                    packet = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(packet, dict):
                    continue
                echo = packet.get("echo")
                if isinstance(echo, str) and echo in self.pending:
                    future = self.pending[echo]
                    if not future.done():
                        future.set_result(packet)
                elif packet.get("post_type") == "message":
                    message = self.normalize(packet)
                    if message:
                        self.auto.incoming(message)
                        try:
                            target = (
                                self.urgent
                                if message.is_mentioned or message.chat_type == "private"
                                else self.queue
                            )
                            target.put_nowait(message)
                            self.ready.set()
                        except asyncio.QueueFull:
                            logger.warning("qq_queue_full", extra={"fields": {"status": "dropped"}})
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(OneBotError("connection_lost_delivery_unknown"))

    async def process(self, message):
        if message.chat_type == "group" and not message.is_mentioned:
            if self.settings.qq_group_context_enabled:
                try:
                    await self.service.observe_group(message)
                    self.auto.submit(message)
                except Exception as exc:  # noqa: BLE001 -- ambient messages never trigger replies
                    logger.error(
                        "qq_observe_failed", extra={"fields": {"error_type": type(exc).__name__}}
                    )
            return
        key = self.settings.qq_self_id + ":" + message.thread_id + ":" + message.message_id
        if not self.ledger.reserve(key):
            logger.info("qq_duplicate_dropped")
            return
        destination = message.chat_type + ":" + message.chat_id
        now = time.monotonic()
        if (
            message.text.split()[0].lower() != "/auto"
            and now - self.last_reply.get(destination, 0) < self.settings.qq_reply_interval_seconds
        ):
            self.ledger.mark(key, "rate_limited")
            return
        self.last_reply[destination] = now
        image_id = None
        try:
            # Group responses are public. Private/global memories and private
            # management commands must never be published to a whole group.
            if (
                message.chat_type == "group"
                and not message.text.startswith("/remember-group ")
                and (
                    message.text.startswith("记住")
                    or message.text.split()[0]
                    in {"/remember", "/remember-global", "/sources", "/history", "/unshare"}
                )
            ):
                content = "个人记忆请私聊管理。群共享信息请明确使用 /remember-group 名称=内容。"
            else:
                if (
                    message.chat_type == "group"
                    and self.settings.qq_group_context_enabled
                    and not message.text.startswith(("/", "记住"))
                ):
                    message = await self.enrich_members(message)
                reply = await self.service.handle(message)
                content = reply.text
                image_id = getattr(reply, "image_id", None)
            segments = [
                {
                    "type": "text",
                    "data": {
                        "text": content[:2000] + ("\n（已截断）" if len(content) > 2000 else "")
                    },
                }
            ]
            if image_id:
                try:
                    raw = self.service.image_source.load(image_id)
                    segments.append(
                        {
                            "type": "image",
                            "data": {"file": "base64://" + base64.b64encode(raw).decode("ascii")},
                        }
                    )
                except Exception:  # noqa: BLE001 -- do not expose local paths or resend expired media
                    segments = [
                        {"type": "text", "data": {"text": "图片缓存已失效，请重新/tag 标签。"}}
                    ]
            async with self.send_lock:
                wait = self.settings.qq_reply_interval_seconds - (time.monotonic() - self.last_sent)
                if wait > 0:
                    await asyncio.sleep(wait)
                # Mark before sending: disconnect after send may mean it was delivered.
                # Never automatically resend after an uncertain acknowledgment.
                self.ledger.mark(key, "delivery_unknown")
                action = "send_group_msg" if message.chat_type == "group" else "send_private_msg"
                target = "group_id" if message.chat_type == "group" else "user_id"
                await self.call(
                    action,
                    {
                        target: int(message.chat_id),
                        "message": segments,
                    },
                )
                self.last_sent = time.monotonic()
            self.ledger.mark(key, "sent")
            if not message.text.startswith("/"):
                self.auto.note_reply(message)
            logger.info(
                "qq_reply_sent",
                extra={"fields": {"status": "ok", "message_id": message.message_id}},
            )
        except Exception as exc:  # noqa: BLE001 -- transport boundary must redact tokens/content
            logger.error("qq_reply_failed", extra={"fields": {"error_type": type(exc).__name__}})

    async def worker(self):
        while True:
            if not self.urgent.empty():
                # Preserve preceding ambient context without running a model or
                # waiting for an automatic draft ahead of an explicit request.
                while not self.queue.empty():
                    ambient = self.queue.get_nowait()
                    try:
                        if self.settings.qq_group_context_enabled:
                            await self.service.observe_group(ambient)
                    finally:
                        self.queue.task_done()
            target = self.urgent if not self.urgent.empty() else self.queue
            if target.empty():
                self.ready.clear()
                await self.ready.wait()
                continue
            message = target.get_nowait()
            self.explicit_busy = message.is_mentioned or message.chat_type == "private"
            try:
                await self.process(message)
            finally:
                self.explicit_busy = False
                target.task_done()

    async def session(self, ws):
        self.ws = ws
        receiver = asyncio.create_task(self.receive(ws))
        worker = None
        try:
            info = await self.call("get_login_info", {})
            if (
                not isinstance(info, dict)
                or stable_id(info.get("user_id")) != self.settings.qq_self_id
            ):
                raise OneBotError("login_identity_mismatch")
            logger.info("qq_connected", extra={"fields": {"status": "ok"}})
            worker = asyncio.create_task(self.worker())
            done, _ = await asyncio.wait([receiver, worker], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            self.ws = None
            await self.auto.close()
            for task in (receiver, worker):
                if task:
                    task.cancel()
                    with suppress(asyncio.CancelledError, OneBotError):
                        await task
            # Do not replay connection-local queued messages after reconnect.
            for queue in (self.queue, self.urgent):
                while not queue.empty():
                    queue.get_nowait()
                    queue.task_done()

    async def run(self):
        self.settings.require_qq()
        delay = 1
        while True:
            try:
                async with connect(
                    self.settings.qq_ws_url,
                    additional_headers={
                        "Authorization": "Bearer "
                        + self.settings.qq_access_token.get_secret_value()
                    },
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=256_000,
                    proxy=None,
                ) as ws:
                    await self.session(ws)
                    delay = 1
            except (OSError, OneBotError) as exc:
                logger.warning(
                    "qq_disconnected", extra={"fields": {"error_type": type(exc).__name__}}
                )
            except Exception as exc:  # noqa: BLE001 -- never print handshake headers/URL
                logger.warning(
                    "qq_connection_failed", extra={"fields": {"error_type": type(exc).__name__}}
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.settings.qq_reconnect_max_seconds)
