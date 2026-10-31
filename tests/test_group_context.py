import asyncio
import json
import time
from contextlib import suppress

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from qq_agent.channels.onebot import OneBotAdapter, OneBotError
from qq_agent.config import Settings
from qq_agent.conversation import member_targets
from qq_agent.messages import GroupMember, IncomingMessage
from qq_agent.model import ModelResponse
from qq_agent.persistence import open_persistence
from qq_agent.service import AgentService
from qq_agent.storage import Database
from qq_agent.tools import default_registry


def settings(tmp_path, **kwargs):
    return Settings(
        _env_file=None,
        database_path=str(tmp_path / "a.db"),
        checkpoint_db_path=str(tmp_path / "c.db"),
        qq_self_id="900",
        qq_allowed_groups=["100"],
        qq_allowed_group_users=["200", "201"],
        qq_allowed_users=["200", "201"],
        qq_private_enabled=True,
        qq_group_context_enabled=True,
        qq_reply_interval_seconds=0,
        **kwargs,
    )


def event(text="我最近在看动画", user=200, group=100, mid="one", mention=False):
    return {
        "post_type": "message",
        "self_id": 900,
        "user_id": user,
        "group_id": group,
        "message_type": "group",
        "message_id": mid,
        "sender": {"user_id": user, "card": "灵明石猴" if user == 200 else "通臂猿猴"},
        "message": ([{"type": "at", "data": {"qq": "900"}}] if mention else [])
        + [{"type": "text", "data": {"text": text}}],
    }


class Client:
    def __init__(self):
        self.requests = []

    async def generate(self, messages, tools=None, tool_choice="auto"):
        self.requests.append(messages)
        return ModelResponse("只根据现有摘录回答")


def envelope(messages):
    return next(
        json.loads(m["content"].split("\n", 1)[1])
        for m in messages
        if m["content"].startswith("以下JSON是渠道")
    )


def test_observation_opt_in_and_scoped_normalization(tmp_path):
    config = settings(tmp_path)
    db = Database(":memory:")
    try:
        adapter = OneBotAdapter(None, config, db)
        incoming = adapter.normalize(event())
        assert incoming and not incoming.is_mentioned
        assert incoming.nickname == "灵明石猴"
        assert incoming.group_members[0].card == "灵明石猴"
        assert adapter.normalize(event(group=101)) is None
        assert adapter.normalize(event(user=202)) is None
        image = event()
        image["message"] = [{"type": "image", "data": {"file": "ignored"}}]
        assert adapter.normalize(image) is None
        config.qq_group_context_enabled = False
        assert adapter.normalize(event()) is None
    finally:
        db.close()


def test_other_member_mentions_are_preserved_as_data(tmp_path):
    db = Database(":memory:")
    try:
        adapter = OneBotAdapter(None, settings(tmp_path), db)
        packet = event("怎么看他的发言", mention=True)
        packet["message"].insert(1, {"type": "at", "data": {"qq": "201"}})
        incoming = adapter.normalize(packet)
        assert incoming.mentioned_users == ("201",)
        assert "@QQ(201)" in incoming.text and incoming.is_mentioned
    finally:
        db.close()


async def test_ambient_text_stores_without_model_reply_or_memory_command(tmp_path):
    config = settings(tmp_path)
    async with open_persistence(config) as p:
        client = Client()
        service = AgentService(client, default_registry(), config, p)
        adapter = OneBotAdapter(service, config, p.database)

        async def no_call(*args):
            pytest.fail("ambient message must not make OneBot actions")

        adapter.call = no_call
        await adapter.process(adapter.normalize(event()))
        await adapter.process(adapter.normalize(event()))
        await adapter.process(adapter.normalize(event("/remember x=private", mid="command")))
        assert not client.requests
        assert (
            p.database.conn.execute("SELECT COUNT(*) FROM group_context_messages").fetchone()[0]
            == 1
        )
        for table in ("memories", "messages", "sessions", "qq_deliveries"):
            assert p.database.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


async def test_member_names_and_targeted_excerpts_reach_model_but_never_private_chat(tmp_path):
    config = settings(tmp_path)
    async with open_persistence(config) as p:
        client = Client()
        service = AgentService(client, default_registry(), config, p)
        adapter = OneBotAdapter(service, config, p.database)
        actions = []

        async def call(action, params):
            actions.append(action)
            if action == "get_group_member_list":
                return [
                    {"group_id": 100, "user_id": 200, "card": "灵明石猴", "nickname": "成员甲"},
                    {"group_id": 100, "user_id": 201, "card": "通臂猿猴"},
                    {"group_id": 999, "user_id": 200, "card": "错误群昵称"},
                    {"group_id": 100, "user_id": 202, "card": "名单外成员"},
                    {"group_id": 100, "user_id": None},
                ]
            return {"message_id": 123}

        adapter.call = call
        await adapter.process(adapter.normalize(event()))
        question = adapter.normalize(
            event("从灵明石猴的发言看，他喜欢什么？", user=201, mid="ask", mention=True)
        )
        await adapter.process(question)
        data = envelope(client.requests[-1])
        assert data["speaker"]["user_id"] == "201"
        assert data["speaker"]["nickname"] == "通臂猿猴"
        assert data["members"][0] == {"user_id": "200", "nickname": "成员甲", "card": "灵明石猴"}
        assert {m["user_id"] for m in data["recent_group_excerpts"]} == {"200"}
        assert data["recent_group_excerpts"][0]["content"] == "我最近在看动画"
        assert "名单外成员" not in json.dumps(data, ensure_ascii=False)
        assert actions == ["get_group_member_list", "send_group_msg"]
        state = await service.graph.aget_state({"configurable": {"thread_id": question.thread_id}})
        assert "recent_group_excerpts" not in json.dumps(state.values, ensure_ascii=False)
        private = IncomingMessage(
            platform="qq",
            chat_type="private",
            chat_id="201",
            user_id="201",
            message_id="private",
            text="灵明石猴说过什么",
        )
        await service.handle(private)
        private_data = envelope(client.requests[-1])
        assert "members" not in private_data and "recent_group_excerpts" not in private_data
        assert "我最近在看动画" not in json.dumps(client.requests[-1], ensure_ascii=False)
        again = await adapter.enrich_members(question)
        assert again.group_members and actions.count("get_group_member_list") == 1


async def test_roster_failure_keeps_observed_identity(tmp_path):
    db = Database(":memory:")
    try:
        adapter = OneBotAdapter(None, settings(tmp_path), db)

        async def failure(*args):
            raise OneBotError("action_failed")

        adapter.call = failure
        message = await adapter.enrich_members(adapter.normalize(event(mention=True)))
        assert message.group_members[0].card == "灵明石猴"
    finally:
        db.close()


async def test_member_list_action_is_confined_to_allowed_group(tmp_path):
    db = Database(":memory:")
    try:
        adapter = OneBotAdapter(None, settings(tmp_path), db)
        for params in ({"group_id": 999}, {"group_id": 100, "user_id": 200}, {"group_id": True}):
            with pytest.raises(OneBotError, match="group_not_allowed"):
                await adapter.call("get_group_member_list", params)
    finally:
        db.close()


def test_nickname_collision_keeps_distinct_member_ids():
    m = IncomingMessage(
        platform="qq",
        chat_type="group",
        chat_id="100",
        user_id="200",
        message_id="1",
        text="同名人的发言",
        group_members=(
            GroupMember(user_id="200", card="同名人"),
            GroupMember(user_id="201", card="同名人"),
        ),
    )
    assert member_targets(m) == ["200", "201"]
    assert member_targets(m.model_copy(update={"text": "1200123"})) == []


def test_storage_blocks_cross_group_private_and_nonmember_reads(tmp_path):
    db = Database(":memory:")
    try:
        adapter = OneBotAdapter(None, settings(tmp_path), db)
        actor = adapter.normalize(event())
        db.observe_group(actor, ["100"], ["200", "201"])
        for denied in (
            actor.model_copy(update={"chat_id": "101"}),
            actor.model_copy(update={"chat_type": "private"}),
            actor.model_copy(update={"user_id": "202"}),
        ):
            assert db.group_context(denied, ["100"], ["200", "201"], 7) == []
            db.observe_group(denied, ["100"], ["200", "201"])
        assert db.group_context(actor, ["100"], ["201"], 7) == []
        assert len(db.group_context(actor, ["100"], ["200", "201"], 7)) == 1
    finally:
        db.close()


async def test_reset_redacts_own_shared_excerpts_and_invalidates_group_snapshots(tmp_path):
    config = settings(tmp_path)
    async with open_persistence(config) as p:
        client = Client()
        service = AgentService(client, default_registry(), config, p)
        adapter = OneBotAdapter(service, config, p.database)
        first = adapter.normalize(event())
        other = adapter.normalize(event("说点什么", user=201, mid="other", mention=True))
        await service.observe_group(first)
        await service.handle(other)
        assert p.database.turns(other)
        reset = adapter.normalize(event("/reset", mid="reset", mention=True))
        await service.handle(reset)
        assert not p.database.turns(other)
        assert not (
            await service.graph.aget_state({"configurable": {"thread_id": other.thread_id}})
        ).values
        # Replaying an old ambient event must not resurrect removed text.
        await service.observe_group(first)
        rows = p.database.group_context(other, ["100"], ["200", "201"], 7)
        assert all(r["user_id"] != "200" for r in rows)


def test_expiry_and_source_deletion_clear_context_and_quoted_snapshots(tmp_path):
    db = Database(":memory:")
    try:
        adapter = OneBotAdapter(None, settings(tmp_path), db)
        source = adapter.normalize(event(mention=True))
        db.touch(source)
        db.observe_group(source, ["100"], ["200", "201"])
        mid = db.save_memory(source, "hobby", "看动画", "group", "group")
        db.change_memory(source, mid, "delete")
        assert not db.group_context(source, ["100"], ["200", "201"], 7)
        ambient = adapter.normalize(event(mid="ambient"))
        db.observe_group(ambient, ["100"], ["200", "201"])
        other = adapter.normalize(event(user=201, mid="reply", mention=True))
        db.touch(other)
        db.complete(other, [[{"role": "assistant", "content": "旧摘录引用"}]], {})
        db.conn.execute(
            "UPDATE group_context_messages SET created_at=?", (time.time() - 7 * 86400 - 1,)
        )
        db.conn.commit()
        db.cleanup(7)
        assert not db.turns(other)
        assert db.conn.execute("SELECT COUNT(*) FROM group_context_messages").fetchone()[0] == 0
    finally:
        db.close()


def test_schema_v1_upgrades_without_removing_existing_data(tmp_path):
    path = tmp_path / "old.db"
    db = Database(path)
    db.conn.execute("INSERT INTO groups VALUES('qq','100',1)")
    db.conn.executescript(
        "DROP TRIGGER group_context_redact; DROP TABLE group_context_messages; PRAGMA user_version=1;"
    )
    db.close()
    db = Database(path)
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.conn.execute("SELECT group_id FROM groups").fetchone()[0] == "100"
        assert db.conn.execute("SELECT COUNT(*) FROM group_context_messages").fetchone()[0] == 0
        assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()


def test_reset_without_shared_excerpts_preserves_other_members_turns(tmp_path):
    db = Database(":memory:")
    try:
        adapter = OneBotAdapter(None, settings(tmp_path), db)
        first = adapter.normalize(event(mention=True))
        other = adapter.normalize(event(user=201, mid="other", mention=True))
        db.touch(first)
        db.touch(other)
        db.complete(other, [[{"role": "assistant", "content": "普通会话"}]], {})
        db.reset(first)
        assert db.turns(other)
    finally:
        db.close()


def test_excerpt_budget_limits_rows_and_text(tmp_path):
    db = Database(":memory:")
    try:
        adapter = OneBotAdapter(None, settings(tmp_path), db)
        for i in range(70):
            actor = adapter.normalize(event("长消息" * 1000, mid=str(i)))
            db.observe_group(actor, ["100"], ["200", "201"])
        rows = db.group_context(actor, ["100"], ["200", "201"], 7)
        assert 0 < len(rows) <= 60
        assert all(len(r["content"]) <= 1200 and r["truncated"] for r in rows)
        assert sum(len(json.dumps(r, ensure_ascii=False)) for r in rows) <= 10000
    finally:
        db.close()


async def test_local_ws_observes_ambient_then_replies_once_with_member_context(tmp_path):
    config = settings(tmp_path)
    actions = []
    replied = asyncio.Event()

    async def server(ws):
        async for raw in ws:
            packet = json.loads(raw)
            action = packet["action"]
            actions.append(action)
            if action == "get_login_info":
                data = {"user_id": 900}
            elif action == "get_group_member_list":
                data = [{"user_id": 200, "card": "灵明石猴"}, {"user_id": 201, "card": "通臂猿猴"}]
            else:
                assert action == "send_group_msg"
                data = {"message_id": 999}
            await ws.send(
                json.dumps({"echo": packet["echo"], "status": "ok", "retcode": 0, "data": data})
            )
            if action == "get_login_info":
                await ws.send(json.dumps(event()))
                await ws.send(json.dumps(event()))
                await ws.send(
                    json.dumps(event("灵明石猴说了什么", user=201, mid="query", mention=True))
                )
            if action == "send_group_msg":
                replied.set()

    async with open_persistence(config) as p, serve(server, "127.0.0.1", 0) as local:
        client = Client()
        service = AgentService(client, default_registry(), config, p)
        adapter = OneBotAdapter(service, config, p.database)
        port = local.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}", proxy=None) as ws:
            task = asyncio.create_task(adapter.session(ws))
            try:
                await asyncio.wait_for(replied.wait(), 5)
                assert actions == ["get_login_info", "get_group_member_list", "send_group_msg"]
                assert len(client.requests) == 1
                assert envelope(client.requests[0])["recent_group_excerpts"][0]["user_id"] == "200"
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
