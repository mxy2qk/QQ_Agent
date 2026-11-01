import asyncio
import json
import time
from contextlib import suppress

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from qq_agent.channels.onebot import OneBotAdapter, OneBotError
from qq_agent.config import Settings
from qq_agent.messages import IncomingMessage
from qq_agent.model import ModelResponse
from qq_agent.persistence import open_persistence
from qq_agent.service import AgentService
from qq_agent.storage import Database
from qq_agent.tools import default_registry


def cfg(tmp_path):
    return Settings(
        _env_file=None,
        database_path=str(tmp_path / "a.db"),
        checkpoint_db_path=str(tmp_path / "c.db"),
        qq_self_id="900",
        qq_allowed_groups=["100"],
        qq_allowed_group_users=["200", "201"],
        qq_control_users=["200"],
        qq_allowed_users=["200"],
        qq_private_enabled=True,
        qq_group_context_enabled=True,
        qq_reply_interval_seconds=0,
    )


def msg(text="有没有人知道今天吃什么？", mid="1", user="200", mention=False, group="100"):
    return IncomingMessage(
        platform="qq",
        chat_type="group",
        chat_id=group,
        user_id=user,
        message_id=mid,
        text=text,
        is_mentioned=mention,
        public_response=True,
    )


class Client:
    def __init__(self, content=None):
        self.content = content or json.dumps(
            {"decision": "reply", "reason_code": "open_question", "reply": "甜" * 100}
        )
        self.calls = []
        self.entered = asyncio.Event()
        self.gate = None

    async def generate(self, messages, tools=None, tool_choice="auto"):
        self.calls.append(messages)
        assert not tools and tool_choice == "none"
        self.entered.set()
        if self.gate:
            await self.gate.wait()
        return ModelResponse(self.content)


@pytest.fixture
async def setup(tmp_path):
    settings = cfg(tmp_path)
    async with open_persistence(settings) as p:
        client = Client()
        service = AgentService(client, default_registry(), settings, p)
        adapter = OneBotAdapter(service, settings, p.database)
        adapter.auto.wait_seconds = 0
        sent = []

        async def call(action, params):
            sent.append((action, params))
            return {"message_id": 123}

        adapter.call = call
        yield adapter, client, sent
        await adapter.auto.close()


async def candidate(adapter, message=None):
    message = message or msg()
    adapter.auto.incoming(message)
    await adapter.process(message)
    task = adapter.auto.tasks.get(message.chat_id)
    if task:
        await task


@pytest.mark.parametrize("mode", ["off", "shadow", "on"])
async def test_modes_quotas_and_length(setup, mode):
    a, c, sent = setup
    if mode != "off":
        assert a.auto.command(msg("/auto " + mode, mention=True, mid="cmd")).status == "ok"
    await candidate(a)
    assert len(c.calls) == (0 if mode == "off" else 1)
    assert len(sent) == (1 if mode == "on" else 0)
    if sent:
        assert len(sent[0][1]["message"][0]["data"]["text"]) <= 70
        assert a.auto.events("100", "send")
    if mode == "shadow":
        assert not a.auto.events("100", "send")


@pytest.mark.parametrize(
    "content",
    [
        "broken",
        "{}",
        json.dumps({"decision": "skip", "reason_code": "answered", "reply": ""}),
        json.dumps({"decision": "reply", "reason_code": "bad", "reply": "x"}),
    ],
)
async def test_skip_and_invalid_model_output_never_send(setup, content):
    a, c, sent = setup
    c.content = content
    a.auto.command(msg("/auto on", mention=True, mid="cmd"))
    await candidate(a)
    assert not sent
    assert not a.ledger.database.turns(msg())


async def test_new_message_and_off_cancel_waiting_judgment(setup):
    a, c, sent = setup
    c.gate = asyncio.Event()
    a.auto.command(msg("/auto on", mention=True, mid="cmd"))
    a.auto.incoming(msg())
    await a.process(msg())
    await asyncio.wait_for(c.entered.wait(), 2)
    a.auto.incoming(msg("好的已经解决", mid="new", user="201"))
    a.auto.command(msg("/auto off", mention=True, mid="off"))
    c.gate.set()
    await a.auto.close()
    assert not sent


async def test_snapshot_change_discards_draft(setup):
    a, c, sent = setup
    c.gate = asyncio.Event()
    a.auto.command(msg("/auto on", mention=True, mid="cmd"))
    a.auto.incoming(msg())
    await a.process(msg())
    await asyncio.wait_for(c.entered.wait(), 2)
    await a.service.handle(msg("/style set 更冷静", mention=True, mid="style"))
    c.gate.set()
    await a.auto.tasks["100"]
    assert not sent


async def test_timeout_is_silent_and_has_no_assistant_turn(setup):
    a, c, sent = setup
    c.gate = asyncio.Event()
    a.auto.timeout_seconds = 0.02
    a.auto.command(msg("/auto on", mention=True, mid="cmd"))
    await candidate(a)
    assert not sent and not a.ledger.database.turns(msg())


async def test_permissions_commands_and_filters(setup):
    a, c, sent = setup
    assert a.auto.command(msg("/auto on", mention=True, user="201")).status == "error"
    assert a.auto.command(msg("/auto on", mention=True, group="999")).status == "error"
    assert a.auto.command(msg("/auto on")).status == "error"
    assert a.auto.command(msg("/auto cooldown 0", mention=True)).status == "error"
    assert a.auto.command(msg("/auto limit 61", mention=True)).status == "error"
    assert a.auto.command(msg("/auto status", mention=True, user="201")).status == "ok"
    for m in (
        msg(user="202"),
        msg(group="999"),
        msg("/style set x"),
        msg("哈哈"),
        msg(mention=True),
    ):
        assert not a.auto.eligible(m)
    assert not c.calls and not sent


async def test_quota_cooldown_duplicate_and_ack_failure(setup):
    a, c, _sent = setup
    a.auto.command(msg("/auto on", mention=True, mid="cmd"))

    async def failure(*args):
        raise OneBotError("unknown_delivery")

    a.call = failure
    await candidate(a)
    assert len(a.auto.events("100", "send")) == 1
    await candidate(a)
    await candidate(a, msg(mid="other"))
    assert len(c.calls) == 1
    assert len(a.auto.events("100", "send")) == 1
    # Simulate a fresh controller after restart, keeping SQL counters.
    replacement = OneBotAdapter(a.service, a.settings, a.ledger.database)
    assert replacement.auto.config("100")["mode"] == "on"
    assert not replacement.auto.budget("100")
    await replacement.auto.close()


async def test_private_data_not_in_auto_prompt_and_followup_limit(setup):
    a, c, _sent = setup
    private = IncomingMessage(
        platform="qq",
        chat_type="private",
        chat_id="200",
        user_id="200",
        message_id="p",
        text="/remember-global secret=DO_NOT_PUBLISH",
    )
    await a.service.handle(private)
    a.auto.command(msg("/auto on", mention=True, mid="cmd"))
    await candidate(a)
    assert "DO_NOT_PUBLISH" not in json.dumps(c.calls)
    a.auto.note_reply(msg())
    assert a.auto.followup(msg())
    a.auto.note_reply(msg(), True)
    a.auto.note_reply(msg(), True)
    assert not a.auto.followup(msg())
    a.auto.incoming(msg(mid="new", user="201"))
    assert not a.auto.followup(msg())


async def test_judge_budget_and_old_candidate(setup):
    a, c, sent = setup
    a.auto.command(msg("/auto on", mention=True, mid="cmd"))
    for i in range(20):
        a.auto.reserve("test" + str(i), "100", "judge")
    assert not a.auto.budget("100")
    a.ledger.database.conn.execute("DELETE FROM auto_events")
    a.ledger.database.conn.commit()
    await a.auto.run(msg(), 0, time.monotonic() - 31)
    assert not c.calls and not sent


async def test_websocket_auto_draft_has_ack_and_no_tools(tmp_path):
    settings = cfg(tmp_path)
    delivered = asyncio.Event()
    actions = []

    async def server(ws):
        async for raw in ws:
            packet = json.loads(raw)
            actions.append(packet["action"])
            data = {"user_id": 900} if packet["action"] == "get_login_info" else {"message_id": 456}
            await ws.send(
                json.dumps({"echo": packet["echo"], "status": "ok", "retcode": 0, "data": data})
            )
            if packet["action"] == "get_login_info":
                await ws.send(
                    json.dumps(
                        {
                            "post_type": "message",
                            "message_type": "group",
                            "self_id": 900,
                            "group_id": 100,
                            "user_id": 200,
                            "message_id": 1,
                            "sender": {"user_id": 200},
                            "message": [{"type": "text", "data": {"text": "然然，今天吃什么？"}}],
                        }
                    )
                )
            else:
                delivered.set()

    async with open_persistence(settings) as p, serve(server, "127.0.0.1", 0) as local:
        client = Client()
        svc = AgentService(client, default_registry(), settings, p)
        adapter = OneBotAdapter(svc, settings, p.database)
        adapter.auto.wait_seconds = 0
        adapter.auto.command(msg("/auto on", mention=True, mid="on"))
        async with connect(f"ws://127.0.0.1:{local.sockets[0].getsockname()[1]}", proxy=None) as ws:
            task = asyncio.create_task(adapter.session(ws))
            try:
                await asyncio.wait_for(delivered.wait(), 3)
                assert actions == ["get_login_info", "send_group_msg"]
                assert len(client.calls) == 1
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


async def test_context_reset_cancels_draft(setup):
    a, c, sent = setup
    c.gate = asyncio.Event()
    a.auto.command(msg("/auto on", mention=True, mid="cmd"))
    a.auto.incoming(msg())
    await a.process(msg())
    await asyncio.wait_for(c.entered.wait(), 2)
    a.ledger.database.reset(msg())
    c.gate.set()
    await a.auto.tasks["100"]
    assert not sent


async def test_followup_uses_completed_public_dialogue_and_reset_removes_it(setup):
    a, _c, _sent = setup
    m = msg()
    a.ledger.database.touch(m)
    a.ledger.database.complete(m, [[{"role": "assistant", "content": "PUBLIC_PREVIOUS_REPLY"}]], {})
    assert "PUBLIC_PREVIOUS_REPLY" in json.dumps(a.auto.snapshot(m)[0])
    a.ledger.database.reset(m)
    assert "PUBLIC_PREVIOUS_REPLY" not in json.dumps(a.auto.snapshot(m)[0])


async def test_rolling_hour_limit_expires(setup):
    a, c, sent = setup
    a.auto.command(msg("/auto on", mention=True, mid="cmd"))
    for i in range(6):
        a.auto.reserve("send" + str(i), "100", "send")
    assert not a.auto.budget("100", True)
    a.ledger.database.conn.execute("UPDATE auto_events SET created_at=?", (time.time() - 3601,))
    a.ledger.database.conn.commit()
    assert a.auto.budget("100")
    assert not c.calls and not sent


def test_v3_migration_preserves_existing_data(tmp_path):
    path = tmp_path / "old.db"
    db = Database(path)
    db.touch(msg())
    mid = db.save_memory(msg(), "hobby", "动画", "group", "group")
    db.conn.executescript(
        "DROP TABLE auto_status; DROP TABLE auto_events; DROP TABLE auto_settings; PRAGMA user_version=3;"
    )
    db.close()
    db = Database(path)
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert db.visible(msg())[0]["id"] == mid
        assert db.conn.execute("SELECT COUNT(*) FROM auto_settings").fetchone()[0] == 0
        assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()
