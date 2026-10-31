from pathlib import Path

import pytest

from qq_agent.command_help import COMMANDS, command_help
from qq_agent.config import Settings
from qq_agent.messages import IncomingMessage
from qq_agent.persistence import open_persistence
from qq_agent.service import AgentService
from qq_agent.tools import default_registry


class NoModel:
    async def generate(self, *args, **kwargs):
        raise AssertionError("help must not invoke a model")


def message(text="/help", group=False, mid="help"):
    return IncomingMessage(
        platform="qq",
        user_id="200",
        chat_type="group" if group else "private",
        chat_id="100" if group else "200",
        message_id=mid,
        text=text,
        public_response=group,
    )


@pytest.mark.parametrize("group", [False, True])
async def test_help_available_without_model_or_persistence(group):
    svc = AgentService(NoModel(), default_registry(), Settings(_env_file=None))
    reply = await svc.handle(message(group=group))
    assert reply.status == "ok" and reply.model_steps == reply.tool_calls == 0
    assert "/memories" in reply.text and "/reset" in reply.text
    assert "时间工具" in reply.text and len(reply.text) < 1900
    if group:
        assert "/remember-group" in reply.text and "个人记忆管理请私聊" in reply.text
        assert "/remember 名称" not in reply.text
    else:
        assert "/remember 名称" in reply.text


@pytest.mark.parametrize("text", ["/help forget", "/help /forget", "/help\tforget"])
async def test_command_specific_help_explains_id_and_side_effects(text):
    svc = AgentService(NoModel(), default_registry(), Settings(_env_file=None))
    reply = await svc.handle(message(text))
    assert "不是列表序号" in reply.text and "不能删除他人" in reply.text


def test_memory_alias_and_unknown_topic():
    settings = Settings(_env_file=None)
    actor = message()
    assert command_help("/memory-help", actor, settings) == command_help(
        "/help memory", actor, settings
    )
    assert "未找到" in command_help("/help missing", actor, settings)
    assert command_help("/helper", actor, settings) is None


def test_skills_help_uses_installed_catalog_without_fetching_data():
    class Manager:
        def catalog(self):
            return "sample-skill: 已安装的测试技能"

    answer = command_help("/help skills", message(), Settings(_env_file=None), Manager())
    assert "sample-skill" in answer and "已关闭" in answer
    assert "MCP" in answer


async def test_persistent_help_does_not_read_or_expose_private_memory(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "a.db"),
        checkpoint_db_path=str(tmp_path / "cp.db"),
    )
    async with open_persistence(settings) as p:
        private = message("/remember secret=do-not-expose", mid="remember")
        svc = AgentService(NoModel(), default_registry(), settings, p)
        await svc.handle(private)

        def forbidden(*args, **kwargs):
            raise AssertionError("help must not retrieve memory")

        svc.memory.retrieve = forbidden
        first = await svc.handle(message(group=True))
        second = await svc.handle(message(group=True))
        assert first == second and "do-not-expose" not in first.text
        assert p.database.turns(message(group=True)) == []
        assert len(p.database.visible(private)) == 1


def test_manual_covers_every_registered_help_command():
    doc = (Path(__file__).parents[1] / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
    for command in COMMANDS:
        assert "/" + command.name in doc


@pytest.mark.parametrize("topic", ["", "memory", "skills", "all"])
def test_help_topics_fit_one_qq_reply(topic):
    answer = command_help("/help " + topic, message(group=True), Settings(_env_file=None))
    assert len(answer) <= 1900


@pytest.mark.parametrize("group", [False, True])
async def test_onebot_help_is_routed_back_as_one_text_reply(tmp_path, group):
    from qq_agent.channels.onebot import OneBotAdapter

    settings = Settings(
        _env_file=None,
        qq_self_id="900",
        qq_allowed_groups=["100"],
        qq_allowed_users=["200"],
        qq_private_enabled=True,
        qq_reply_interval_seconds=0,
        database_path=str(tmp_path / "a.db"),
        checkpoint_db_path=str(tmp_path / "cp.db"),
    )
    async with open_persistence(settings) as p:
        svc = AgentService(NoModel(), default_registry(), settings, p)
        adapter = OneBotAdapter(svc, settings, p.database)
        sent = []

        async def call(action, params):
            sent.append((action, params))
            return {"message_id": 123}

        adapter.call = call
        event = {
            "post_type": "message",
            "message_type": "group" if group else "private",
            "self_id": 900,
            "user_id": 200,
            "group_id": 100,
            "message_id": 1,
            "sub_type": "normal" if group else "friend",
            "sender": {"user_id": 200},
            "message": ([{"type": "at", "data": {"qq": "900"}}] if group else [])
            + [{"type": "text", "data": {"text": " /help"}}],
        }
        await adapter.process(adapter.normalize(event))
        assert len(sent) == 1
        assert sent[0][0] == ("send_group_msg" if group else "send_private_msg")
        content = sent[0][1]["message"]
        assert len(content) == 1 and content[0]["type"] == "text"
        assert "/memories" in content[0]["data"]["text"]
        assert "（已截断）" not in content[0]["data"]["text"]


def test_bare_group_mention_opens_help_but_image_only_does_not(tmp_path):
    from qq_agent.channels.onebot import OneBotAdapter
    from qq_agent.storage import Database

    settings = Settings(_env_file=None, qq_self_id="900", qq_allowed_groups=["100"])
    db = Database(tmp_path / "a.db")
    try:
        adapter = OneBotAdapter(None, settings, db)
        event = {
            "post_type": "message",
            "message_type": "group",
            "self_id": 900,
            "user_id": 200,
            "group_id": 100,
            "message_id": 1,
            "sender": {"user_id": 200},
            "message": [{"type": "at", "data": {"qq": "900"}}],
        }
        assert adapter.normalize(event).text == "/help"
        event["message"].append({"type": "image", "data": {"file": "not-read"}})
        assert adapter.normalize(event) is None
    finally:
        db.close()
