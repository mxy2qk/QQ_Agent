import asyncio
import json
import time
from uuid import uuid4

import pytest

from qq_agent.config import Settings
from qq_agent.memory import MemoryExtractor
from qq_agent.messages import IncomingMessage
from qq_agent.model import ModelResponse
from qq_agent.persistence import open_persistence
from qq_agent.service import AgentService
from qq_agent.storage import Database, MemoryAccessError
from qq_agent.tools import default_registry


def actor(text="你好", user="a", group="g", platform="cli", mid=None, private=False):
    return IncomingMessage(
        platform=platform,
        user_id=user,
        chat_id=group,
        chat_type="private" if private else "group",
        message_id=mid or str(uuid4()),
        text=text,
    )


def settings(tmp_path, **kwargs):
    return Settings(
        _env_file=None,
        database_path=str(tmp_path / "agent.db"),
        checkpoint_db_path=str(tmp_path / "checkpoint.db"),
        **kwargs,
    )


class EchoClient:
    def __init__(self, delay=0):
        self.requests = []
        self.delay = delay

    async def generate(self, messages, tools=None, tool_choice="auto"):
        self.requests.append(list(messages))
        await asyncio.sleep(self.delay)
        memories = [m["content"] for m in messages if "以下 JSON" in str(m.get("content"))]
        return ModelResponse("授权内容：" + " ".join(memories))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "agent.db")
    yield database
    database.close()


def save(db, who, content="学习 LangGraph", scope="private", key="goal", kind="personal", **kwargs):
    db.touch(who)
    return db.save_memory(who, key, content, scope, kind, **kwargs)


def test_six_users_and_cross_group_platform_isolation(db):
    for user, content in zip(
        "abcdef", ["LangGraph", "B站游戏", "贴吧故事", "跑步", "摄影", "音乐"]
    ):
        save(db, actor(user=user), content)
    assert len(db.visible(actor(user="a"))) == 1
    assert db.visible(actor(user="a"))[0]["content"] == "LangGraph"
    assert db.visible(actor(user="b"))[0]["content"] == "B站游戏"
    assert db.visible(actor(user="a", group="other")) == []
    assert db.visible(actor(user="a", platform="qq")) == []
    assert db.conn.execute("SELECT count(*) FROM users").fetchone()[0] == 6
    assert db.conn.execute("SELECT count(*) FROM group_members").fetchone()[0] == 6


def test_share_revoke_and_group_memory_owner_control(db):
    a, b = actor(), actor(user="b")
    mid = save(db, a)
    db.touch(b)
    assert db.visible(b) == []
    db.change_memory(a, mid, "share")
    assert db.visible(b)[0]["id"] == mid
    with pytest.raises(MemoryAccessError):
        db.change_memory(b, mid, "delete")
    other = actor(user="b", group="other")
    db.touch(other)
    assert db.visible(other) == []
    db.change_memory(a, mid, "unshare")
    assert db.visible(b) == []
    gid = save(db, a, "周五读书会", "group", "meeting", "group")
    assert db.visible(b)[0]["id"] == gid
    with pytest.raises(MemoryAccessError):
        db.change_memory(b, gid, "correct", "撤销")
    db.change_memory(a, gid, "delete")
    assert db.visible(b) == []


def test_global_is_owner_only_not_public_or_implicit_platform_link(db):
    save(db, actor(), "喜欢 Python", "global")
    assert len(db.visible(actor(group="different"))) == 1
    assert len(db.visible(actor(private=True))) == 1
    assert db.visible(actor(user="b")) == []
    assert db.visible(actor(platform="qq")) == []


def test_update_dedup_and_correction_updates_fts_and_sources(db):
    a = actor("记住学习目标")
    mid = save(db, a, "学习 LangGraph")
    assert save(db, actor(), "学习 Python") == mid
    assert len(db.visible(a)) == 1
    assert db.visible(a, "LangGraph") == []
    assert db.visible(a, "Python")[0]["id"] == mid
    correction = actor("/correct 内容")
    db.touch(correction)
    db.change_memory(correction, mid, "correct", "研究机器学习")
    assert db.visible(a, "机器学习")[0]["id"] == mid
    assert db.visible(a, "机器")[0]["id"] == mid
    assert db.visible(a, "Python") == []
    assert db.sources(a, mid)[-1]["source_message_id"] == correction.message_id
    assert (
        db.conn.execute(
            "SELECT content FROM messages WHERE message_id=?", (a.message_id,)
        ).fetchone()
        is None
    )


def test_delete_removes_fts_sources_consents_and_source_text(db):
    a = actor("记住我的学习 LangGraph")
    mid = save(db, a)
    db.change_memory(a, mid, "delete")
    assert db.visible(a, "LangGraph") == []
    for table in ["memories", "memories_fts", "memory_sources", "consent_records", "messages"]:
        assert db.conn.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
    assert db.reply(a)["text"] == "该历史事件已处理，相关上下文已清理。"


def test_retrieval_injection_never_bypasses_permission(db):
    save(db, actor(), "super secret LangGraph")
    b = actor(user="b")
    db.touch(b)
    for query in ['" OR 1=1 --', "LangGraph", "*", "secret OR LangGraph"]:
        assert db.visible(b, query) == []


def test_expiry_removes_memory_and_7_day_history_but_keeps_explicit_memory(db):
    a = actor()
    expired = save(db, a, "过期记忆", key="expired", expires_at=time.time() - 1)
    permanent = save(db, a, "长期学习目标", key="permanent")
    with db.transaction() as conn:
        conn.execute("UPDATE messages SET created_at=?", (time.time() - 8 * 86400,))
    db.cleanup(7)
    assert [r["id"] for r in db.visible(a)] == [permanent]
    assert db.conn.execute("SELECT * FROM memories WHERE id=?", (expired,)).fetchone() is None
    assert db.conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    assert db.sources(a, permanent)[0]["source_message_id"] == a.message_id


def test_commands_are_explicit_not_inferred():
    extractor = MemoryExtractor()
    assert extractor.extract("我喜欢 Python") is None
    assert extractor.extract("今天好累") is None
    assert extractor.extract("记住：我喜欢 Python").scope == "private"
    assert extractor.extract("/remember-group 兴趣=Python").kind == "group"


async def test_service_injects_only_current_user_memory_for_six_members(tmp_path):
    config = settings(tmp_path)
    async with open_persistence(config) as p:
        client = EchoClient()
        svc = AgentService(client, default_registry(), config, p)
        for user in "abcdef":
            result = await svc.handle(actor(f"/remember goal=unique-{user}", user=user))
            assert "已保存" in result.text
        for user in "abcdef":
            reply = await svc.handle(actor("我的目标是什么？", user=user))
            assert reply.status == "ok", reply.text
            request = json.dumps(client.requests[-1], ensure_ascii=False)
            assert f"unique-{user}" in request
            for other in set("abcdef") - {user}:
                assert f"unique-{other}" not in request


async def test_restart_restores_completed_history_checkpoint_and_dedup(tmp_path):
    config = settings(tmp_path)
    first = actor("第一轮消息", mid="fixed")
    async with open_persistence(config) as p:
        client = EchoClient()
        svc = AgentService(client, default_registry(), config, p)
        reply = await svc.handle(first)
        assert reply.status == "ok"
        snapshot = await svc.graph.aget_state({"configurable": {"thread_id": first.thread_id}})
        assert snapshot.values["reply"] == reply.text
        assert not snapshot.next
    async with open_persistence(config) as p:
        client = EchoClient()
        svc = AgentService(client, default_registry(), config, p)
        assert await svc.handle(first) == reply
        assert client.requests == []
        await svc.handle(actor("第二轮消息"))
        assert any(m.get("content") == "第一轮消息" for m in client.requests[-1])
        assert len(p.database.turns(first)) == 2


async def test_revoke_erases_other_members_cached_answers_and_checkpoints(tmp_path):
    config = settings(tmp_path)
    async with open_persistence(config) as p:
        client = EchoClient()
        svc = AgentService(client, default_registry(), config, p)
        await svc.handle(actor("/remember fact=shared-secret"))
        await svc.handle(actor("/share 1"))
        b_query = actor("群里有什么记忆？", user="b")
        assert "shared-secret" in (await svc.handle(b_query)).text
        await svc.handle(actor("/unshare 1"))
        snapshot = await svc.graph.aget_state({"configurable": {"thread_id": b_query.thread_id}})
        assert snapshot.values == {}
        assert "shared-secret" not in (await svc.handle(b_query)).text
        await svc.handle(actor("群里有什么记忆？", user="b"))
        assert "shared-secret" not in json.dumps(client.requests[-1], ensure_ascii=False)


async def test_delete_then_restart_cannot_recover_memory_or_replay_original_save(tmp_path):
    config = settings(tmp_path)
    remember = actor("/remember goal=deleted-secret", mid="remember-id")
    async with open_persistence(config) as p:
        svc = AgentService(EchoClient(), default_registry(), config, p)
        await svc.handle(remember)
        await svc.handle(actor("我的目标"))
        await svc.handle(actor("/forget 1"))
    async with open_persistence(config) as p:
        client = EchoClient()
        svc = AgentService(client, default_registry(), config, p)
        replay = await svc.handle(remember)
        assert "已处理" in replay.text
        await svc.handle(actor("我的目标"))
        assert "deleted-secret" not in json.dumps(client.requests[-1], ensure_ascii=False)
        assert p.database.visible(remember) == []


async def test_retention_deletes_checkpoint_and_history(tmp_path):
    config = settings(tmp_path)
    a = actor("expired message")
    async with open_persistence(config) as p:
        svc = AgentService(EchoClient(), default_registry(), config, p)
        await svc.handle(a)
        with p.database.transaction() as db:
            db.execute("UPDATE messages SET created_at=?", (time.time() - 8 * 86400,))
        await svc.cleanup()
        assert p.database.turns(a) == []
        snapshot = await svc.graph.aget_state({"configurable": {"thread_id": a.thread_id}})
        assert snapshot.values == {}


async def test_pending_purge_finishes_after_restart(tmp_path):
    config = settings(tmp_path)
    a = actor()
    async with open_persistence(config) as p:
        svc = AgentService(EchoClient(), default_registry(), config, p)
        await svc.handle(a)
        # Simulate crash after transactional invalidation but before saver deletion.
        p.database.reset(a)
    async with open_persistence(config) as p:
        svc = AgentService(EchoClient(), default_registry(), config, p)
        assert (
            await svc.graph.aget_state({"configurable": {"thread_id": a.thread_id}})
        ).values == {}


async def test_process_lock_prevents_second_database_owner(tmp_path):
    config = settings(tmp_path)
    async with open_persistence(config):
        with pytest.raises(ValueError, match="另一个 Agent"):
            async with open_persistence(config):
                pass


async def test_persistent_timeout_does_not_commit_partial_turn(tmp_path):
    config = settings(tmp_path, agent_timeout_seconds=0.05)
    async with open_persistence(config) as p:
        svc = AgentService(EchoClient(delay=0.2), default_registry(), config, p)
        who = actor()
        assert (await svc.handle(who)).status == "error"
        assert p.database.turns(who) == []


async def test_private_group_commands_and_mutation_permissions(tmp_path):
    config = settings(tmp_path)
    async with open_persistence(config) as p:
        svc = AgentService(EchoClient(), default_registry(), config, p)
        assert "只能在群会话" in (await svc.handle(actor("/remember-group x=y", private=True))).text
        await svc.handle(actor("/remember x=private-note"))
        assert "无权" in (await svc.handle(actor("/correct 1 hacked", user="b"))).text
        assert p.database.visible(actor())[0]["content"] == "private-note"


def test_history_search_owner_only_and_delete_removes_index(db):
    a = actor("记住私有事实 Python")
    mid = save(db, a, "Python")
    b = actor(user="b")
    db.touch(b)
    assert db.search_messages(b, "Python") == []
    assert db.search_messages(a, "Python")[0]["message_id"] == a.message_id
    db.change_memory(a, mid, "delete")
    assert db.search_messages(a, "Python") == []


def test_same_key_update_erases_previous_source_text(db):
    first = actor("旧的学习记录")
    save(db, first, "旧目标")
    save(db, actor("新的学习记录"), "新目标")
    assert db.search_messages(first, "旧的学习记录") == []
    assert db.visible(first)[0]["content"] == "新目标"


async def test_global_correction_from_other_chat_and_extraction_disabled(tmp_path):
    config = settings(tmp_path)
    async with open_persistence(config) as p:
        svc = AgentService(EchoClient(), default_registry(), config, p)
        await svc.handle(actor("/remember-global preference=简短回答"))
        await svc.handle(actor("/correct 1 详细回答", group="other"))
        assert p.database.visible(actor())[0]["content"] == "详细回答"
        assert "只能切换当前群" in (await svc.handle(actor("/share 1"))).text
        assert p.database.visible(actor())[0]["scope"] == "global"
        disabled = AgentService(
            EchoClient(), default_registry(), settings(tmp_path, memory_extraction_enabled=False), p
        )
        assert "已关闭" in (await disabled.handle(actor("/remember x=y"))).text
        assert len(p.database.visible(actor())) == 1


async def test_group_delete_clears_all_reader_history_not_other_groups(tmp_path):
    config = settings(tmp_path)
    async with open_persistence(config) as p:
        svc = AgentService(EchoClient(), default_registry(), config, p)
        await svc.handle(actor("/remember-group meeting=周五读书会"))
        b, other = actor(user="b"), actor(user="b", group="other")
        await svc.handle(b)
        await svc.handle(other)
        await svc.handle(actor("/forget 1"))
        assert p.database.turns(b) == []
        assert len(p.database.turns(other)) == 1


async def test_concurrent_persistent_duplicate_runs_model_once(tmp_path):
    config = settings(tmp_path)
    async with open_persistence(config) as p:
        client = EchoClient(delay=0.01)
        svc = AgentService(client, default_registry(), config, p)
        a = actor()
        first, second = await asyncio.gather(svc.handle(a), svc.handle(a))
        assert first == second and len(client.requests) == 1


def test_schema_reopen_and_foreign_key_integrity(tmp_path):
    path = tmp_path / "agent.db"
    db = Database(path)
    save(db, actor())
    db.close()
    db = Database(path)
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.visible(actor())[0]["content"] == "学习 LangGraph"
    finally:
        db.close()


async def test_completed_checkpoints_are_compacted_without_duplicating_turns(tmp_path):
    config = settings(tmp_path)
    async with open_persistence(config) as p:
        svc = AgentService(EchoClient(), default_registry(), config, p)
        who = actor()
        for _ in range(3):
            await svc.handle(actor())
        snapshot = await svc.graph.aget_state({"configurable": {"thread_id": who.thread_id}})
        assert len(snapshot.values["messages"]) == 6
        async with p.checkpointer.conn.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id=?", (who.thread_id,)
        ) as cursor:
            assert (await cursor.fetchone())[0] == 1


async def test_display_sequence_restarts_after_delete_without_reusing_memory_id(tmp_path):
    config = settings(tmp_path)
    async with open_persistence(config) as p:
        svc = AgentService(EchoClient(), default_registry(), config, p)
        await svc.handle(actor("/remember first=旧记忆"))
        await svc.handle(actor("/forget 1"))
        saved = await svc.handle(actor("/remember second=新记忆"))
        assert "ID #2" in saved.text
        listing = await svc.handle(actor("/memories"))
        assert "序号 1｜ID #2" in listing.text
        assert "序号 2｜" not in listing.text
        assert "修改、删除请用 ID" in listing.text
        # A delayed /forget 1 must not erase the newly created memory.
        denied = await svc.handle(actor("/forget 1"))
        assert "无权" in denied.text or "不存在" in denied.text
        assert p.database.visible(actor())[0]["content"] == "新记忆"


async def test_filtered_memory_list_has_contiguous_display_sequence(tmp_path):
    config = settings(tmp_path)
    async with open_persistence(config) as p:
        svc = AgentService(EchoClient(), default_registry(), config, p)
        await svc.handle(actor("/remember a=Python基础"))
        await svc.handle(actor("/remember b=其他目标"))
        await svc.handle(actor("/remember c=Python进阶"))
        listing = await svc.handle(actor("/memories Python"))
        assert "序号 1｜ID #3" in listing.text
        assert "序号 2｜ID #1" in listing.text
        assert "其他目标" not in listing.text
