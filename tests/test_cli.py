import json
import logging
import os
import subprocess
import sys

from qq_agent.observability import JsonFormatter, request_context


def invoke(*args, input_text=None):
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "DEEPSEEK_API_KEY": "",
        "DEEPSEEK_MODEL": "",
        "DEEPSEEK_BASE_URL": "",
    }
    return subprocess.run(
        [sys.executable, "main.py", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=20,
        check=False,
        input=input_text,
    )


def test_demo_cli_real_time_without_credentials():
    result = invoke("--demo", "--message", "现在几点？")
    assert result.returncode == 0
    assert "离线演示" in result.stdout and "Asia/Shanghai" in result.stdout
    assert "tool_completed" in result.stderr


def test_cli_missing_config_has_clear_error_without_traceback():
    result = invoke("--message", "你好")
    assert result.returncode == 2
    assert "DEEPSEEK_API_KEY" in result.stderr
    assert "Traceback" not in result.stderr


def test_interactive_chinese_input_reset_and_exit():
    result = invoke("--demo", input_text="你好\n现在几点？\n/reset\n/exit\n")
    assert result.returncode == 0
    assert "Asia/Shanghai" in result.stdout
    assert "已清空当前会话" in result.stdout


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
