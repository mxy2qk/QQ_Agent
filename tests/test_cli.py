import json
import logging
import os
import subprocess
import sys

from qq_agent.observability import JsonFormatter, request_context


async def test_verify_api_uses_text_request_without_tools(capsys):
    from qq_agent.cli import verify_live
    from qq_agent.model import ModelResponse

    class Client:
        async def generate(self, messages, tools=None, tool_choice="auto"):
            assert tools is None
            return ModelResponse("你好")

    await verify_live(Client())
    assert "普通文本请求" in capsys.readouterr().out


def invoke(*args, input_text=None, ephemeral=True):
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "DEEPSEEK_API_KEY": "",
        "DEEPSEEK_MODEL": "",
        "DEEPSEEK_BASE_URL": "",
        "PUBLIC_SOURCES_ENABLED": "false",
    }
    return subprocess.run(
        [sys.executable, "main.py", *(["--ephemeral"] if ephemeral else []), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=20,
        check=False,
        input=input_text,
    )


def test_demo_cli_without_credentials():
    result = invoke("--demo", "--message", "你好")
    assert result.returncode == 0
    assert "离线演示" in result.stdout
    assert "tool_completed" not in result.stderr


def test_cli_missing_config_has_clear_error_without_traceback():
    result = invoke("--message", "你好")
    assert result.returncode == 2
    assert "DEEPSEEK_API_KEY" in result.stderr
    assert "Traceback" not in result.stderr


def test_source_verification_does_not_require_model_credentials():
    result = invoke("--verify-sources", ephemeral=False)
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert not report["complete"]
    assert all(r["error"] == "source_disabled" for r in report["results"].values())
    assert "DEEPSEEK_API_KEY" not in result.stderr


def test_interactive_chinese_input_reset_and_exit():
    result = invoke("--demo", input_text="你好\n现在几点？\n/reset\n/exit\n")
    assert result.returncode == 0
    assert "离线演示" in result.stdout
    assert "已清空当前会话" in result.stdout


def test_cli_memory_survives_process_restart_and_is_deleted(tmp_path):
    args = ["--demo", "--data-dir", str(tmp_path / "cli-data"), "--user", "test-member"]
    result = invoke(*args, "--message", "/remember goal=LangGraph", ephemeral=False)
    assert result.returncode == 0 and "已保存记忆（ID #1" in result.stdout
    result = invoke(*args, "--message", "/memories", ephemeral=False)
    assert result.returncode == 0 and "LangGraph" in result.stdout
    result = invoke(*args, "--message", "/forget 1", ephemeral=False)
    assert result.returncode == 0 and "操作完成" in result.stdout
    result = invoke(*args, "--message", "/memories", ephemeral=False)
    assert result.returncode == 0 and "没有可访问的记忆" in result.stdout


def test_log_has_context_without_private_message():
    token = request_context.set({"request_id": "request-1", "message_id": "message-1"})
    try:
        record = logging.LogRecord("qq_agent", logging.INFO, "", 1, "model_started", (), None)
        result = json.loads(JsonFormatter().format(record))
        assert result["request_id"] == "request-1"
        assert result["event"] == "model_started"
        assert "content" not in result and "api_key" not in result
    finally:
        request_context.reset(token)
