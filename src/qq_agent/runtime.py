"""Local Windows process manager. No QQ messages or credentials in control files."""

import argparse
import asyncio
import ctypes
import json
import logging
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
RETRY_DELAYS = (3, 10)
STABLE_SECONDS = 300
STOP_SECONDS = 20


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        # A status reader can briefly hold the destination open on Windows.
        for attempt in range(10):
            try:
                temporary.replace(path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.05)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class Paths:
    root: Path

    @property
    def run(self):
        return self.root / "data" / "run"

    @property
    def state(self):
        return self.run / "runtime.json"

    @property
    def worker(self):
        return self.run / "runtime-worker.json"

    @property
    def stop(self):
        return self.run / "runtime-stop.json"


@contextmanager
def file_lock(path):
    """Same byte-range locking convention as the application database locks."""
    import msvcrt

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("操作已在进行，或数据库正被另一个 Agent 使用。") from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _process_handle(pid, terminate=False):
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    kernel.QueryFullProcessImageNameW.argtypes = [
        w.HANDLE,
        w.DWORD,
        w.LPWSTR,
        ctypes.POINTER(w.DWORD),
    ]
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
    handle = kernel.OpenProcess(0x1000 | 0x100000 | (1 if terminate else 0), False, int(pid))
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # invalid PID: already exited
            return kernel, None
        raise OSError(error, "无法核实进程身份，未操作该进程")
    return kernel, handle


def _identity(kernel, handle, pid):
    from ctypes import wintypes as w

    if kernel.WaitForSingleObject(handle, 0) == 0:
        return None
    times = [w.FILETIME() for _ in range(4)]
    buffer, length = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
    if not kernel.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
        raise ctypes.WinError(ctypes.get_last_error())
    return {
        "pid": pid,
        "created": (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime,
        "image": buffer.value.casefold(),
    }


def identity(pid=None):
    pid = os.getpid() if pid is None else pid
    kernel, handle = _process_handle(pid)
    if not handle:
        return None
    try:
        return _identity(kernel, handle, pid)
    finally:
        kernel.CloseHandle(handle)


def alive(reference):
    return bool(reference and identity(reference["pid"]) == reference)


def terminate(reference):
    """Verify creation time and image on the SAME handle used for termination."""
    if not reference:
        return
    kernel, handle = _process_handle(reference["pid"], terminate=True)
    if not handle:
        return
    try:
        if _identity(kernel, handle, reference["pid"]) == reference:
            if not kernel.TerminateProcess(handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            kernel.WaitForSingleObject(handle, 5000)
    finally:
        kernel.CloseHandle(handle)


def hidden_spawn(command, root, **kwargs):
    return subprocess.Popen(
        command,
        cwd=root,
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
        **kwargs,
    )


def stop_requested(paths, token):
    return read_json(paths.stop).get("token") == token


def current_worker(paths, state):
    worker = read_json(paths.worker)
    if (worker.get("token"), worker.get("generation")) == (
        state.get("token"),
        state.get("generation"),
    ):
        return worker
    return {}


def port_open(settings):
    from urllib.parse import urlsplit

    url = urlsplit(settings.qq_ws_url)
    try:
        with socket.create_connection(
            (url.hostname, url.port or (443 if url.scheme == "wss" else 80)), 1
        ):
            return True
    except OSError:
        return False


def ensure_napcat(paths, settings):
    """Only start the bundled local launcher; never stop QQ or change its login."""
    from urllib.parse import urlsplit

    if port_open(settings):
        return "NapCat 端口可访问；登录与鉴权以 Agent 连接状态为准。"
    host = urlsplit(settings.qq_ws_url).hostname
    launcher = paths.root / "start-napcat.cmd"
    installation = paths.root / "napcat" / "v4.18.42"
    if (
        host not in {"127.0.0.1", "localhost", "::1"}
        or not (installation / "launcher.bat").is_file()
    ):
        return "NapCat 尚不可连接，请先启动并登录；Agent 会自动等待重连。"
    # Avoid launching a second copy while an existing console is awaiting login.
    # Process command lines are only inspected locally and never printed or persisted.
    environment = {**os.environ, "QQ_AGENT_NAPCAT_FOLDER": str(installation)}
    check = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$p=$env:QQ_AGENT_NAPCAT_FOLDER; try { "
                "$found=@(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object { "
                "$_.CommandLine -and $_.CommandLine.IndexOf($p,[StringComparison]::OrdinalIgnoreCase) "
                "-ge 0 }); if ($found.Count -gt 0) { exit 0 } else { exit 1 } } catch { exit 2 }"
            ),
        ],
        check=False,
        env=environment,
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
        timeout=10,
    )
    if check.returncode != 1:
        return "检测到 NapCat 启动进程或无法核实状态，请检查原登录窗口；Agent 会等待重连。"
    subprocess.Popen(
        ["cmd.exe", "/d", "/c", str(launcher)],
        cwd=paths.root,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    return "已打开 NapCat 登录窗口，需要时请扫码；Agent 会自动等待连接。"


def preflight(paths):
    from qq_agent.config import Settings

    if not (paths.root / ".venv" / "Scripts" / "python.exe").is_file():
        raise RuntimeError("缺少项目 .venv，请先按 README 安装环境。")
    settings = Settings()
    settings.require_live()
    settings.require_qq()
    # Detect manually started instances without opening or modifying their databases.
    for name in (settings.database_path, settings.checkpoint_db_path):
        with file_lock(Path(name).resolve().with_suffix(Path(name).suffix + ".lock")):
            pass
    return settings


def stop_runtime(paths):
    state = read_json(paths.state)
    if not state.get("token"):
        return
    write_json(paths.stop, {"token": state["token"]})
    deadline = time.monotonic() + STOP_SECONDS
    while time.monotonic() < deadline:
        worker = current_worker(paths, state)
        if not alive(state.get("supervisor")) and not alive(worker.get("identity")):
            return
        time.sleep(0.2)
    # Worker shutdown normally cancels the request and closes SQLite. Only a
    # bounded timeout permits forced termination, and never by PID alone.
    terminate(current_worker(paths, state).get("identity"))
    terminate(state.get("supervisor"))
    state.update(phase="stopped", stopped_at=time.time())
    write_json(paths.state, state)


def start_runtime(paths):
    state = read_json(paths.state)
    if alive(state.get("supervisor")) and state.get("phase") != "paused":
        print("守护程序已运行，无需重复启动。")
        return
    if alive(state.get("supervisor")) or alive(current_worker(paths, state).get("identity")):
        stop_runtime(paths)
    settings = preflight(paths)
    print(ensure_napcat(paths, settings))
    token = uuid4().hex
    write_json(paths.state, {"token": token, "phase": "launching"})
    with (paths.run / "supervisor-console.log").open("w", encoding="utf-8") as log:
        hidden_spawn(
            [sys.executable, "-m", "qq_agent.runtime", "_supervise", "--token", token],
            paths.root,
            stdout=log,
            stderr=log,
        )
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        state = read_json(paths.state)
        worker = current_worker(paths, state)
        if state.get("phase") == "paused":
            raise RuntimeError("启动已暂停；运行“查看状态”检查错误，修正后再次启动。")
        if state.get("phase") == "running" and alive(worker.get("identity")):
            print("Agent 已启动并由守护程序管理。")
            return
        time.sleep(0.2)
    raise RuntimeError("启动尚未就绪，请查看状态及 data/run/supervisor-console.log。")


def retry_delay(failures, elapsed, code):
    """Configuration/startup errors stop immediately; unstable crashes are bounded."""
    failures = 0 if elapsed >= STABLE_SECONDS else failures
    failures += 1
    delay = None if code == 2 or failures > len(RETRY_DELAYS) else RETRY_DELAYS[failures - 1]
    return failures, delay


def supervise(paths, token):
    logger = logging.getLogger("qq_agent.supervisor")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(paths.run / "supervisor.log", maxBytes=1_000_000, backupCount=2)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    state = {"token": token, "supervisor": identity(), "phase": "starting", "failures": 0}
    child = None
    with file_lock(paths.run / "supervisor.lock"):
        try:
            while not stop_requested(paths, token):
                state.update(phase="starting", generation=uuid4().hex, retry_at=None)
                write_json(paths.state, state)
                start = time.monotonic()
                with (paths.run / "managed-agent.stderr.log").open("w", encoding="utf-8") as err:
                    child = hidden_spawn(
                        [
                            sys.executable,
                            "-m",
                            "qq_agent.runtime",
                            "_worker",
                            "--token",
                            token,
                            "--generation",
                            state["generation"],
                        ],
                        paths.root,
                        stdout=err,
                        stderr=err,
                    )
                while child.poll() is None and not stop_requested(paths, token):
                    if state["phase"] != "running" and alive(
                        current_worker(paths, state).get("identity")
                    ):
                        state["phase"] = "running"
                        write_json(paths.state, state)
                    time.sleep(0.2)
                if stop_requested(paths, token):
                    break
                code = child.wait()
                state["failures"], delay = retry_delay(
                    state["failures"], time.monotonic() - start, code
                )
                state.update(
                    exit_code=code,
                    phase="paused" if delay is None else "retrying",
                    retry_at=None if delay is None else time.time() + delay,
                )
                logger.warning(
                    "worker_exit code=%s failures=%s phase=%s",
                    code,
                    state["failures"],
                    state["phase"],
                )
                write_json(paths.state, state)
                if delay is None:
                    while not stop_requested(paths, token):
                        time.sleep(0.2)
                    break
                until = time.monotonic() + delay
                while time.monotonic() < until and not stop_requested(paths, token):
                    time.sleep(0.2)
        finally:
            write_json(paths.stop, {"token": token})
            if child and child.poll() is None:
                try:
                    child.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    terminate(current_worker(paths, state).get("identity"))
                    child.terminate()  # Popen still owns its launcher; never a recycled PID.
                    child.wait(timeout=5)
            state.update(phase="stopped", stopped_at=time.time())
            write_json(paths.state, state)
            logger.info("supervisor_stopped")
            handler.close()
            logger.removeHandler(handler)


class WorkerEvents(logging.Handler):
    def __init__(self, paths, state):
        super().__init__()
        self.paths, self.state = paths, state

    def emit(self, record):
        event = record.getMessage()
        fields = getattr(record, "fields", {})
        if event not in {"qq_connected", "qq_disconnected", "qq_connection_failed"}:
            if record.levelno < logging.WARNING:
                return
            # Only stable event names belong in the process status file.
            if not event.isascii() or not event.replace("_", "").isalnum():
                event = "application_warning"
        if event == "qq_connected":
            self.state.update(connected=True, connection_event=event, connection_at=time.time())
        elif event in {"qq_disconnected", "qq_connection_failed"}:
            self.state.update(connected=False, connection_event=event, connection_at=time.time())
        if record.levelno >= logging.WARNING:
            self.state["last_error"] = {
                "event": event,
                "type": fields.get("error_type"),
                "at": time.time(),
            }
        self.state["updated_at"] = time.time()
        write_json(self.paths.worker, self.state)


async def run_worker(paths, token, generation):
    from qq_agent import cli

    state = {
        "token": token,
        "generation": generation,
        "identity": identity(),
        "connected": False,
        "started_at": time.time(),
    }
    write_json(paths.worker, state)
    handler = WorkerEvents(paths, state)
    args = argparse.Namespace(
        qq=True,
        demo=False,
        ephemeral=False,
        message=None,
        group=None,
        user="local-user",
        data_dir=None,
        verify_api=False,
        verify_sources=False,
        list_skills=False,
    )
    task = asyncio.create_task(cli.run(args, log_handler=handler))
    try:
        while not task.done():
            if stop_requested(paths, token):
                task.cancel()
                break
            await asyncio.sleep(0.2)
        with suppress(asyncio.CancelledError):
            return await task or 0
        return 0
    except Exception as exc:  # noqa: BLE001 -- never serialize config/SDK exception contents
        state["last_error"] = {
            "event": "worker_failed",
            "type": type(exc).__name__,
            "at": time.time(),
        }
        print("Agent 启动/运行失败，错误类型：" + type(exc).__name__, file=sys.stderr)
        from pydantic import ValidationError

        from qq_agent.config import ConfigurationError

        return 2 if isinstance(exc, (ConfigurationError, ValidationError)) else 1
    finally:
        state.update(connected=False, stopped_at=time.time())
        write_json(paths.worker, state)


def show_status(paths):
    from qq_agent.config import Settings

    state = read_json(paths.state)
    worker = current_worker(paths, state)
    supervisor_alive = alive(state.get("supervisor"))
    worker_alive = alive(worker.get("identity"))
    names = {
        "launching": "启动中",
        "starting": "启动中",
        "running": "运行中",
        "retrying": "等待自动重启",
        "paused": "重试已暂停",
        "stopped": "已停止",
    }
    print("守护程序：" + ("运行中" if supervisor_alive else "未运行"))
    print(
        "Agent："
        + (
            names.get(state.get("phase"), "未知")
            if supervisor_alive
            else ("仍运行（守护程序已退出）" if worker_alive else "已停止")
        )
    )
    if worker_alive:
        print("Agent PID：" + str(worker["identity"]["pid"]))
    print(
        "QQ连接：" + ("已登录并通过鉴权" if worker_alive and worker.get("connected") else "未连接")
    )
    try:
        print("NapCat端口：" + ("可访问" if port_open(Settings()) else "不可访问"))
    except Exception:  # noqa: BLE001 -- config values must stay local and redacted
        print("NapCat端口：配置无效，无法检查")
    if state.get("retry_at") and supervisor_alive:
        print("预计重试等待：" + str(max(0, round(state["retry_at"] - time.time()))) + "秒")
    error = worker.get("last_error")
    if error:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(error["at"]))
        print(f"最近错误（{when}）：{error['event']} / {error.get('type') or '-'}")
    if state.get("phase") == "paused":
        print("修正配置或检查日志后，双击“启动机器人”重新尝试。")
    print("日志：logs/agent.log；data/run/supervisor.log；data/run/managed-agent.stderr.log")
    try:
        import sqlite3

        config = Settings()
        with sqlite3.connect(
            Path(config.database_path).resolve().as_uri() + "?mode=ro", uri=True
        ) as db:
            modes = [
                (
                    db.execute(
                        "SELECT mode FROM auto_settings WHERE group_id=?", (group,)
                    ).fetchone()
                    or ("off",)
                )[0]
                for group in config.qq_allowed_groups
            ]
        print("选择性接话：" + ("、".join(modes) if modes else "无授权群"))
    except (OSError, ValueError, sqlite3.Error):
        print("选择性接话：尚未初始化")


def main():
    from pydantic import ValidationError

    from qq_agent.config import ConfigurationError

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="本项目 Windows Agent 管理")
    parser.add_argument(
        "action", choices=["start", "stop", "restart", "status", "_supervise", "_worker"]
    )
    parser.add_argument("--token")
    parser.add_argument("--generation")
    args = parser.parse_args()
    paths = Paths(ROOT)
    os.chdir(paths.root)
    paths.run.mkdir(parents=True, exist_ok=True)
    try:
        if os.name != "nt":
            raise RuntimeError("管理脚本仅支持 Windows；其他系统请直接运行 qq-agent --qq。")
        if args.action == "_supervise" and args.token:
            supervise(paths, args.token)
        elif args.action == "_worker" and args.token and args.generation:
            return asyncio.run(run_worker(paths, args.token, args.generation))
        elif args.action == "status":
            show_status(paths)
        elif not args.action.startswith("_"):
            with file_lock(paths.run / "control.lock"):
                if args.action in {"stop", "restart"}:
                    stop_runtime(paths)
                    print("Agent 与守护程序已停止，NapCat 保持运行。")
                if args.action in {"start", "restart"}:
                    start_runtime(paths)
                    show_status(paths)
        else:
            raise RuntimeError("内部启动参数不完整。")
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors())
        print("配置字段无效：" + fields, file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- never dump secrets from validation/SDK/OS errors
        print("管理操作失败，错误类型：" + type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
