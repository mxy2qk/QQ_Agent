import asyncio
import logging
import os
import subprocess
import sys
import threading
import time

import pytest

from qq_agent import runtime


def eventually(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        time.sleep(0.05)
    raise AssertionError("runtime did not reach expected state")


def test_retry_policy_has_limit_and_resets_after_stable_run():
    assert runtime.retry_delay(0, 1, 1) == (1, 3)
    assert runtime.retry_delay(1, 1, 1) == (2, 10)
    assert runtime.retry_delay(2, 1, 1) == (3, None)
    assert runtime.retry_delay(2, 301, 1) == (1, 3)
    assert runtime.retry_delay(0, 1, 2) == (1, None)


def test_control_tokens_and_generations_exclude_old_files(tmp_path):
    paths = runtime.Paths(tmp_path)
    runtime.write_json(paths.worker, {"token": "old", "generation": "g", "connected": True})
    state = {"token": "new", "generation": "g"}
    assert runtime.current_worker(paths, state) == {}
    runtime.write_json(paths.worker, {"token": "new", "generation": "previous"})
    assert runtime.current_worker(paths, state) == {}
    runtime.write_json(paths.stop, {"token": "old"})
    assert not runtime.stop_requested(paths, "new")


def test_status_observer_tracks_authenticated_connection_and_redacts_fields(tmp_path):
    paths = runtime.Paths(tmp_path)
    state = {"connected": False}
    observer = runtime.WorkerEvents(paths, state)
    record = logging.LogRecord("qq_agent", logging.INFO, "", 1, "qq_connected", (), None)
    observer.emit(record)
    assert runtime.read_json(paths.worker)["connected"]
    record = logging.LogRecord("qq_agent", logging.WARNING, "", 1, "qq_disconnected", (), None)
    record.fields = {"error_type": "OSError", "token": "do-not-store", "content": "private"}
    observer.emit(record)
    saved = paths.worker.read_text(encoding="utf-8")
    assert not state["connected"]
    assert state["last_error"]["type"] == "OSError"
    assert "do-not-store" not in saved and "private" not in saved


@pytest.mark.asyncio
async def test_worker_stop_cancels_request_and_runs_cleanup(tmp_path, monkeypatch):
    from qq_agent import cli

    paths = runtime.Paths(tmp_path)
    finished = asyncio.Event()
    entered = asyncio.Event()

    async def fake_run(args, log_handler):
        assert args.qq and not args.ephemeral
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(runtime, "identity", lambda: {"pid": 123})
    task = asyncio.create_task(runtime.run_worker(paths, "token", "gen"))
    await asyncio.wait_for(entered.wait(), 2)
    runtime.write_json(paths.stop, {"token": "token"})
    assert await asyncio.wait_for(task, 2) == 0
    assert finished.is_set()
    assert runtime.read_json(paths.worker)["stopped_at"]


@pytest.mark.asyncio
async def test_worker_config_error_is_fatal_and_redacted(tmp_path, monkeypatch, capsys):
    from qq_agent import cli
    from qq_agent.config import ConfigurationError

    async def fake_run(args, log_handler):
        raise ConfigurationError("secret-value")

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(runtime, "identity", lambda: {"pid": 123})
    paths = runtime.Paths(tmp_path)
    assert await runtime.run_worker(paths, "token", "gen") == 2
    assert "secret-value" not in paths.worker.read_text(encoding="utf-8")
    assert "secret-value" not in capsys.readouterr().err


@pytest.mark.skipif(os.name != "nt", reason="Windows process management")
def test_creation_identity_prevents_terminating_reused_pid(tmp_path):
    pid_file = tmp_path / "pid.json"
    script = (
        "import time; from pathlib import Path; from qq_agent.runtime import identity,write_json; "
        "write_json(Path(__import__('sys').argv[1]),identity()); time.sleep(60)"
    )
    child = runtime.hidden_spawn([sys.executable, "-c", script, str(pid_file)], tmp_path)
    reference = eventually(lambda: runtime.read_json(pid_file))
    try:
        runtime.terminate({**reference, "created": reference["created"] + 1})
        assert runtime.alive(reference)
        runtime.terminate(reference)
        eventually(lambda: not runtime.alive(reference))
        child.wait(timeout=5)
    finally:
        runtime.terminate(reference)
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


FAKE_WORKER = """
import sys,time
from pathlib import Path
from qq_agent.runtime import Paths,identity,write_json,stop_requested
p=Paths(Path(sys.argv[1])); token,generation,mode=sys.argv[2:]
write_json(p.worker,dict(token=token,generation=generation,identity=identity()))
if mode=='crash':
    time.sleep(.2)
    sys.exit(1)
while not stop_requested(p,token): time.sleep(.05)
"""


@pytest.mark.skipif(os.name != "nt", reason="Windows process management")
@pytest.mark.parametrize("mode", ["crash", "wait"])
def test_supervisor_restarts_bounds_failures_and_obeys_manual_stop(tmp_path, monkeypatch, mode):
    paths = runtime.Paths(tmp_path)
    paths.run.mkdir(parents=True)
    launched = []
    errors = []

    def spawn(command, root, **kwargs):
        child = subprocess.Popen(
            [sys.executable, "-c", FAKE_WORKER, str(root), "token", command[-1], mode],
            cwd=root,
            creationflags=subprocess.CREATE_NO_WINDOW,
            **kwargs,
        )
        launched.append(child)
        return child

    def supervise():
        try:
            runtime.supervise(paths, "token")
        except Exception as exc:  # noqa: BLE001 -- propagate background test failures to assertions
            errors.append(exc)

    monkeypatch.setattr(runtime, "hidden_spawn", spawn)
    monkeypatch.setattr(runtime, "RETRY_DELAYS", (0.01, 0.01))
    thread = threading.Thread(target=supervise)
    thread.start()
    try:
        if mode == "crash":
            eventually(lambda: runtime.read_json(paths.state).get("phase") == "paused")
            assert len(launched) == 3
            time.sleep(0.4)
            assert len(launched) == 3
        else:
            eventually(lambda: runtime.read_json(paths.state).get("phase") == "running")
        runtime.write_json(paths.stop, {"token": "token"})
        thread.join(timeout=15)
        assert not thread.is_alive() and not errors
        assert runtime.read_json(paths.state)["phase"] == "stopped"
        assert all(p.poll() is not None for p in launched)
        count = len(launched)
        time.sleep(0.3)
        assert len(launched) == count
    finally:
        runtime.write_json(paths.stop, {"token": "token"})
        thread.join(timeout=15)


def test_duplicate_start_does_not_spawn_or_touch_napcat(tmp_path, monkeypatch, capsys):
    paths = runtime.Paths(tmp_path)
    runtime.write_json(paths.state, {"phase": "running", "supervisor": {"pid": 123}})
    monkeypatch.setattr(runtime, "alive", lambda ref: bool(ref))
    monkeypatch.setattr(runtime, "hidden_spawn", lambda *a, **k: pytest.fail("duplicate launch"))
    runtime.start_runtime(paths)
    assert "无需重复启动" in capsys.readouterr().out


def test_napcat_already_listening_is_not_restarted(tmp_path, monkeypatch):
    from qq_agent.config import Settings

    monkeypatch.setattr(runtime, "port_open", lambda _: True)
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: pytest.fail("QQ restarted"))
    assert "可访问" in runtime.ensure_napcat(runtime.Paths(tmp_path), Settings(_env_file=None))
