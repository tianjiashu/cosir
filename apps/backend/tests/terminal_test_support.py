"""终端单元测试共用替身：按 worker 协议喂事件的假 worker 与 service 构造。

假 worker 不模拟 ANSI 渲染：测试直接注入已渲染的屏幕行与滚出历史（物理行，软换行行不带
``\\n``），真实渲染由 Rust 与 PTY 集成测试覆盖。
"""

from __future__ import annotations

import base64
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.tools.terminal_runtime.session_service import TerminalSessionService


def terminal_worker_path() -> Path:
    """真实 worker 二进制：环境变量优先，否则用本仓 ``cargo build`` 的调试产物。"""

    configured = os.environ.get("CODING_AGENT_TERMINAL_WORKER")
    if configured:
        return Path(configured)
    name = "terminal-worker.exe" if os.name == "nt" else "terminal-worker"
    return Path(__file__).resolve().parents[3] / "target" / "debug" / name


# 真跑命令的用例（execute_terminal 与交互终端）都经 worker 启动进程：没构建时跳过。
requires_terminal_worker = pytest.mark.skipif(
    not terminal_worker_path().is_file(), reason="terminal-worker binary is not built"
)

class FakeWorker:
    pid = 4321
    rows = 24
    columns = 80
    integration_ready = False

    def __init__(self) -> None:
        self.geometry = {"rows": self.rows, "columns": self.columns}
        self.callback: Callable[[dict[str, Any]], None] | None = None
        self.writes: list[dict[str, Any]] = []
        self.resizes: list[tuple[int, int]] = []
        self.closed = False
        self.on_write: Callable[[FakeWorker, dict[str, Any]], None] | None = None
        self.instance_id = ""

    def start(self, spec: Any, cwd: str, on_event: Callable[[dict[str, Any]], None]) -> None:
        self.callback = on_event
        if self.integration_ready:
            on_event({"type": "shell", "event": {"kind": "ready", "shell": "zsh", "version": "5"}})
        else:
            on_event({"type": "shell", "event": {"kind": "unavailable", "reason": "test"}})

    def write(self, input: dict[str, Any]) -> None:
        self.writes.append(dict(input))
        if self.on_write is not None:
            self.on_write(self, dict(input))

    def resize(self, rows: int, columns: int) -> None:
        self.resizes.append((rows, columns))

    def close(self) -> None:
        self.closed = True

    # ---- 事件注入 ----

    def emit(self, event: dict[str, Any]) -> None:
        assert self.callback is not None
        self.callback(event)

    def output(self, data: bytes = b"x") -> None:
        self.emit({"type": "output", "data_base64": base64.b64encode(data).decode("ascii")})

    def screen(
        self,
        rows: list[str | tuple[str, bool]],
        *,
        cursor: tuple[int, int] = (0, 0),
        history: list[str] | None = None,
        alternate: bool = False,
        size: tuple[int, int] | None = None,
    ) -> None:
        height, width = size or (self.rows, self.columns)
        lines = [
            {"text": row, "wrapped": False} if isinstance(row, str)
            else {"text": row[0], "wrapped": row[1]}
            for row in rows
        ]
        lines += [{"text": "", "wrapped": False}] * (height - len(lines))
        self.emit(
            {
                "type": "screen",
                "screen": {
                    "rows": height,
                    "columns": width,
                    "lines": lines,
                    "cursor": {"row": cursor[0], "column": cursor[1]},
                    "alternate": alternate,
                },
                "history": history or [],
            }
        )

    def shell(self, kind: str, **fields: Any) -> None:
        self.emit({"type": "shell", "event": {"kind": kind, **fields}})

    def exit(self, code: int = 0) -> None:
        self.emit({"type": "exit", "reason": "exited", "exit_code": code})


class ReadyWorker(FakeWorker):
    integration_ready = True


def make_service(
    monkeypatch: Any,
    *,
    worker_type: type[FakeWorker] = FakeWorker,
    on_changed: Callable[[int], None] | None = None,
) -> tuple[TerminalSessionService, list[FakeWorker]]:
    """只在具体构造边界替换 worker，收集每个会话独立创建的替身。"""

    workers: list[FakeWorker] = []

    def construct(instance_id: str) -> FakeWorker:
        worker = worker_type()
        worker.instance_id = instance_id
        workers.append(worker)
        return worker

    monkeypatch.setattr(
        "app.tools.terminal_runtime.session_runtime.ProcessTerminalWorker", construct
    )
    monkeypatch.setattr(
        "app.tools.terminal_runtime.session_service.ShellResolver.resolve",
        lambda shell, integration="auto", **_: _spec(shell, integration),
    )
    capture_terminal_effects(monkeypatch, on_changed=on_changed)
    return TerminalSessionService(), workers


def capture_terminal_effects(
    monkeypatch: Any,
    *,
    on_changed: Callable[[int], None] | None = None,
) -> None:
    """替换会话对应用层的副作用（snapshot 重投影）；会话本身不触达其他应用层动作。"""
    from app.tools.terminal_runtime import session_runtime

    monkeypatch.setattr(
        session_runtime, "refresh_task_sessions", on_changed or (lambda _task: None)
    )


def _spec(shell: str, integration: str) -> Any:
    from app.tools.terminal_runtime.shell_resolver import ShellSpec

    kind = "zsh" if shell == "auto" else shell
    return ShellSpec(kind, f"/bin/{kind}", (f"/bin/{kind}",), integration)


def start(service: TerminalSessionService, tmp_path: Any, *, task_id: int = 1,
          shell: str = "auto") -> str:
    runtime = service.start(
        task_id=task_id,
        workspace_root=str(tmp_path),
        shell=shell,
    )
    return runtime.session_id


def user_enter(runtime: Any, text: str = "") -> None:
    """以用户身份敲一行（原文 + 回车）：与终端 WS 的 raw 帧同一路径，Agent 不再有这条路径。"""
    assert runtime.user_input((text + "\r").encode()) == "accepted"


def wait_for_integration(runtime: Any, timeout: float = 5.0) -> None:
    """真 worker 的 shell 集成异步就绪：带超时轮询到 ready，供需要提示符的用例使用。"""
    wait_until(lambda: runtime.snapshot().integration == "ready", timeout=timeout)


def never() -> bool:
    return False


def session_view(service: TerminalSessionService, session_id: str, task_id: int = 1) -> Any:
    """测试检查用：会话当前快照。"""
    return service.require(session_id, task_id).snapshot()


def session_entries(service: TerminalSessionService, task_id: int = 1) -> list[dict[str, Any]]:
    """该 Task 的 Transport 会话条目（生产投影同一路径：快照 → ``to_terminal_session``）。"""
    from app.assistant_transport.state.conversation_state_terminal_session import (
        to_terminal_session,
    )

    return [dict(to_terminal_session(r.snapshot())) for r in service.task_runtimes(task_id)]


def geometry(service: TerminalSessionService, session_id: str, task_id: int = 1) -> tuple[int, int]:
    """测试检查用：会话当前屏幕尺寸（跟随 worker screen 事件）。"""
    buffer = service.require(session_id, task_id)._observation.buffer
    return buffer.rows, buffer.columns


def active_session_ids(service: TerminalSessionService, task_id: int = 1) -> list[str]:
    return [
        str(entry["sessionId"])
        for entry in session_entries(service, task_id)
        if entry["status"] in ("starting", "running")
    ]


def run_in_thread(target: Callable[[], Any]) -> tuple[threading.Thread, dict[str, Any]]:
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["result"] = target()
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.01)


def run_local_command(
    command: str,
    cwd: Any,
    timeout: float,
    *,
    shell: str = "auto",
) -> tuple[Any, Any]:
    """直接跑 ``LocalExecutionBackend``（不经工具层），返回 (结局, 输出汇总)。

    输出归调用方持有的 ``CommandOutput``，所以直接调后端的测试也要自己建一个再读它。
    """
    from app.tools.tool_handler.terminal.command_output import CommandOutput
    from app.tools.tool_handler.terminal.local_backend import LocalExecutionBackend

    output = CommandOutput(workspace_root=Path(cwd))
    result = LocalExecutionBackend().execute(command, str(cwd), timeout, output, shell=shell)
    return result, output.summary()
