import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from qq_agent.channels.onebot import OneBotAdapter, OneBotError
from qq_agent.config import ConfigurationError, Settings
from qq_agent.messages import IncomingMessage
from qq_agent.model import ModelResponse
from qq_agent.persistence import open_persistence
from qq_agent.service import AgentReply, AgentService
from qq_agent.storage import Database
from qq_agent.tools import default_registry


def settings(**kwargs):
    return Settings(
        _env_file=None,
        qq_adapter_mode="onebot",
        qq_self_id="900",
        qq_allowed_groups=["100"],
        qq_allowed_users=["200"],
        qq_private_enabled=True,
        qq_reply_interval_seconds=0,
        qq_action_timeout_seconds=0.2,
        **kwargs,
    )


def event(mid=1, user=200, group=100, mention=True, private=False):
    return {
        "post_type": "message",
        "self_id": 900,
        "user_id": user,
        "message_id": mid,
        "group_id": group,
        "message_type": "private" if private else "group",
        "sub_type": "friend" if private else "normal",
        "sender": {"user_id": user, "nickname": "昵称"},
        "message": ([{"type": "at", "data": {"qq": "900"}}] if mention else [])
        + [{"type": "text", "data": {"text": "你好"}}],
    }


class Service:
    def __init__(self):
        self.messages = []

    async def handle(self, message):
        self.messages.append(message)
        return AgentReply("文本 [CQ:at,qq=all]", "ok")


@pytest.fixture
def adapter(tmp_path):
    db = Database(tmp_path / "agent.db")
    result = OneBotAdapter(Service(), settings(), db)
    yield result
    db.close()


def test_normalize_mentions_identity_and_namespaces(adapter):
    message = adapter.normalize(event())
    assert message.platform == "qq" and message.user_id == "200" and message.chat_id == "100"
    assert message.is_mentioned and message.public_response and message.text == "你好"
    assert adapter.normalize(event(mention=False)) is None
    assert adapter.normalize(event(user=900)) is None
    assert adapter.normalize(event(group=999)) is None
    assert adapter.normalize(event(user=201, private=True)) is None
    private = adapter.normalize(event(private=True))
    assert private.chat_type == "private" and not private.public_response
    assert private.thread_id != message.thread_id


@pytest.mark.parametrize("private_enabled", [False, True])
def test_group_member_allowlist_and_private_mode(adapter, private_enabled):
    adapter.settings.qq_allowed_group_users = ["200", "201", "202", "203"]
    adapter.settings.qq_allowed_users = ["200", "201", "202", "203"]
    adapter.settings.qq_private_enabled = private_enabled
    for user in (200, 201, 202, 203):
        assert adapter.normalize(event(user=user)) is not None
        assert adapter.normalize(event(user=user, mention=False)) is None
        assert adapter.normalize(event(user=user, group=999)) is None
        assert bool(adapter.normalize(event(user=user, private=True))) == private_enabled
    assert adapter.normalize(event(user=204)) is None
    assert adapter.normalize(event(user=204, private=True)) is None


def test_group_and_private_member_filters_are_independent(adapter):
    adapter.settings.qq_allowed_group_users = ["201"]
    assert adapter.normalize(event(user=200)) is None
    assert adapter.normalize(event(user=201)) is not None
    assert adapter.normalize(event(user=200, private=True)) is not None
    assert adapter.normalize(event(user=201, private=True)) is None
    adapter.settings.qq_allowed_group_users = []
    assert adapter.normalize(event(user=204)) is not None


def test_group_member_allowlist_validation():
    cfg = settings(
        qq_allowed_group_users=["not-an-id"],
        qq_live_enabled=True,
        qq_ws_url="ws://127.0.0.1:3001/",
        qq_access_token="local-test-token",
    )
    with pytest.raises(ConfigurationError, match="白名单"):
        cfg.require_qq()


@pytest.mark.parametrize(
    "change",
    [
        {"self_id": 999},
        {"post_type": "message_sent"},
        {"anonymous": {"id": 1}},
        {"message": "[CQ:at,qq=900]你好"},
        {"message": [{"type": "text", "data": {"text": "[CQ:at,qq=900]你好"}}]},
        {"sender": {"user_id": 999}},
        {"user_id": True},
        {"message_id": None},
        {"message": [{"type": "image", "data": {"file": "https://private.invalid"}}]},
    ],
)
def test_rejects_untrusted_or_unsupported_events(adapter, change):
    assert adapter.normalize({**event(), **change}) is None


async def test_duplicate_and_plain_text_send_route(adapter):
    sent = []

    async def call(action, params):
        sent.append((action, params))
        return {"message_id": 10}

    adapter.call = call
    message = adapter.normalize(event())
    await adapter.process(message)
    await adapter.process(message)
    assert len(sent) == 1 and len(adapter.service.messages) == 1
    assert sent[0][0] == "send_group_msg" and sent[0][1]["group_id"] == 100
    assert sent[0][1]["message"] == [{"type": "text", "data": {"text": "文本 [CQ:at,qq=all]"}}]
    await adapter.process(adapter.normalize(event(mid=2, private=True)))
    assert sent[-1][0] == "send_private_msg" and sent[-1][1]["user_id"] == 200


async def test_uncertain_delivery_never_retries_even_after_adapter_restart(adapter):
    calls = []

    async def fail(*args):
        calls.append(args)
        raise OneBotError("connection_lost_delivery_unknown")

    adapter.call = fail
    message = adapter.normalize(event())
    await adapter.process(message)
    second = OneBotAdapter(adapter.service, adapter.settings, adapter.ledger.database)
    second.call = fail
    await second.process(message)
    assert len(calls) == 1
    row = adapter.ledger.database.conn.execute("SELECT status FROM qq_deliveries").fetchone()
    assert row[0] == "delivery_unknown"


async def test_rate_limit_and_private_command_guard(adapter):
    sent = []

    async def call(action, params):
        sent.append(params)

    adapter.call = call
    first = adapter.normalize(event())
    await adapter.process(first.model_copy(update={"text": "/remember secret=password"}))
    assert adapter.service.messages == []
    assert "私聊" in sent[0]["message"][0]["data"]["text"]
    adapter.settings.qq_reply_interval_seconds = 60
    await adapter.process(adapter.normalize(event(mid=2)))
    assert len(sent) == 1


async def test_ws_echo_auth_handshake_and_dispatch_local_only(adapter):
    actions = []
    got_reply = asyncio.Event()

    async def server(ws):
        assert ws.request.headers["Authorization"] == "Bearer local-test-token"
        async for raw in ws:
            data = json.loads(raw)
            actions.append(data)
            if data["action"] == "get_login_info":
                await ws.send(
                    json.dumps(
                        {
                            "status": "ok",
                            "retcode": 0,
                            "echo": data["echo"],
                            "data": {"user_id": 900},
                        }
                    )
                )
                await ws.send(json.dumps(event()))
                await ws.send(json.dumps(event()))
            else:
                await ws.send(
                    json.dumps(
                        {
                            "status": "ok",
                            "retcode": 0,
                            "echo": data["echo"],
                            "data": {"message_id": 44},
                        }
                    )
                )
                got_reply.set()

    async with serve(server, "127.0.0.1", 0) as local:
        port = local.sockets[0].getsockname()[1]
        adapter.settings.qq_ws_url = f"ws://127.0.0.1:{port}"
        adapter.settings.qq_live_enabled = True
        from pydantic import SecretStr

        adapter.settings.qq_access_token = SecretStr("local-test-token")
        task = asyncio.create_task(adapter.run())
        try:
            async with asyncio.timeout(3):
                await got_reply.wait()
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert [r["action"] for r in actions] == ["get_login_info", "send_group_msg"]


def test_live_config_requires_explicit_enable_and_secure_endpoint():
    with pytest.raises(ConfigurationError, match="QQ_LIVE_ENABLED"):
        settings().require_qq()
    config = settings(qq_live_enabled=True, qq_ws_url="ws://192.0.2.1:3001", qq_access_token="test")
    with pytest.raises(ConfigurationError, match="本机"):
        config.require_qq()
    config.qq_ws_url = "ws://127.0.0.1:3001/"
    config.require_qq()


async def test_six_users_private_context_never_injected_into_public_group(tmp_path):
    config = settings(
        database_path=str(tmp_path / "a.db"), checkpoint_db_path=str(tmp_path / "cp.db")
    )

    class Client:
        def __init__(self):
            self.requests = []

        async def generate(self, messages, tools=None, tool_choice="auto"):
            self.requests.append(messages)
            return ModelResponse("公开回答")

    async with open_persistence(config) as p:
        client = Client()
        svc = AgentService(client, default_registry(), config, p)
        for user in range(200, 206):
            private = IncomingMessage(
                platform="qq",
                user_id=str(user),
                chat_type="private",
                chat_id=str(user),
                message_id=f"p{user}",
                text=f"/remember-global secret=private-{user}",
            )
            await svc.handle(private)
        adapter = OneBotAdapter(svc, config, p.database)
        for user in range(200, 206):
            group = adapter.normalize(event(mid=user, user=user))
            await svc.handle(group)
            context = json.dumps(client.requests[-1])
            assert "private-" not in context
        who = IncomingMessage(
            platform="qq",
            user_id="200",
            chat_type="private",
            chat_id="200",
            message_id="query",
            text="我的记忆",
        )
        await svc.handle(who)
        assert "private-200" in json.dumps(client.requests[-1])
        assert "private-201" not in json.dumps(client.requests[-1])


async def test_action_timeout_and_management_action_rejection(adapter):
    class WS:
        def __init__(self):
            self.sent = []

        async def send(self, raw):
            self.sent.append(json.loads(raw))

    adapter.ws = WS()
    with pytest.raises(OneBotError, match="action_timeout"):
        await adapter.call("send_group_msg", {"group_id": 100, "message": []})
    assert adapter.pending == {}
    with pytest.raises(OneBotError, match="action_not_allowed"):
        await adapter.call("set_group_ban", {})
    assert len(adapter.ws.sent) == 1


async def test_reconnect_after_disconnect_and_no_duplicate_send(adapter):
    from pydantic import SecretStr

    connections = 0
    delivered = asyncio.Event()

    async def server(ws):
        nonlocal connections
        connections += 1
        raw = json.loads(await ws.recv())
        await ws.send(
            json.dumps(
                {"status": "ok", "retcode": 0, "echo": raw["echo"], "data": {"user_id": 900}}
            )
        )
        if connections == 1:
            await ws.close()
            return
        await ws.send(json.dumps(event()))
        reply = json.loads(await ws.recv())
        await ws.send(
            json.dumps(
                {"status": "ok", "retcode": 0, "echo": reply["echo"], "data": {"message_id": 99}}
            )
        )
        delivered.set()
        await ws.wait_closed()

    async with serve(server, "127.0.0.1", 0) as local:
        adapter.settings.qq_ws_url = f"ws://127.0.0.1:{local.sockets[0].getsockname()[1]}"
        adapter.settings.qq_access_token = SecretStr("test-token")
        adapter.settings.qq_live_enabled = True
        task = asyncio.create_task(adapter.run())
        try:
            async with asyncio.timeout(4):
                await delivered.wait()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert connections == 2


def test_nickname_change_does_not_change_identity_and_private_can_be_disabled(adapter):
    original = adapter.normalize(event())
    changed = event()
    changed["sender"]["nickname"] = "新昵称"
    assert adapter.normalize(changed).thread_id == original.thread_id
    adapter.settings.qq_private_enabled = False
    assert adapter.normalize(event(private=True)) is None


async def test_public_group_memories_only_even_for_same_owner(tmp_path):
    config = settings(
        database_path=str(tmp_path / "a.db"), checkpoint_db_path=str(tmp_path / "cp.db")
    )
    async with open_persistence(config) as p:
        public = IncomingMessage(
            platform="qq",
            user_id="200",
            chat_type="group",
            chat_id="100",
            message_id="1",
            text="hello",
            public_response=True,
        )
        p.database.touch(public)
        p.database.save_memory(public, "shared", "群约定", scope="group", kind="group")
        private = public.model_copy(update={"public_response": False})
        p.database.save_memory(private, "personal", "本人私有信息")
        assert len(p.database.visible(private)) == 2
        assert [r["content"] for r in p.database.visible(public)] == ["群约定"]


async def test_login_mismatch_never_starts_message_worker(adapter):
    from websockets.asyncio.client import connect

    actions = []

    async def server(ws):
        packet = json.loads(await ws.recv())
        actions.append(packet["action"])
        await ws.send(
            json.dumps(
                {"status": "ok", "retcode": 0, "echo": packet["echo"], "data": {"user_id": 999}}
            )
        )
        await ws.send(json.dumps(event()))
        await ws.wait_closed()

    async with (
        serve(server, "127.0.0.1", 0) as local,
        connect(f"ws://127.0.0.1:{local.sockets[0].getsockname()[1]}", proxy=None) as ws,
    ):
        with pytest.raises(OneBotError, match="identity_mismatch"):
            await adapter.session(ws)
    assert actions == ["get_login_info"]
    assert adapter.service.messages == []


async def test_reset_command_clears_history_but_preserves_explicit_memory(tmp_path):
    config = settings(
        database_path=str(tmp_path / "a.db"), checkpoint_db_path=str(tmp_path / "cp.db")
    )

    class Client:
        async def generate(self, *args, **kwargs):
            return ModelResponse("你好")

    async with open_persistence(config) as p:
        svc = AgentService(Client(), default_registry(), config, p)
        private = IncomingMessage(
            platform="qq",
            user_id="200",
            chat_type="private",
            chat_id="200",
            message_id="save",
            text="/remember study=Python",
        )
        await svc.handle(private)
        await svc.handle(private.model_copy(update={"message_id": "chat", "text": "你好"}))
        assert p.database.turns(private)
        reply = await svc.handle(
            private.model_copy(update={"message_id": "reset", "text": "/reset"})
        )
        assert "清空" in reply.text
        assert p.database.turns(private) == []
        assert p.database.visible(private)[0]["content"] == "Python"
