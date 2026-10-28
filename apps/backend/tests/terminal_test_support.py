"""终端单元测试共用替身：按 worker 协议喂事件的假 worker 与 service 构造。

假 worker 不模拟 ANSI 渲染：测试直接注入已渲染的屏幕行与滚出历史（物理行，软换行行不带
``\\n``），真实渲染由 Rust 与 PTY 集成测试覆盖。
"""

from __future__ import annotations

import base64
import threading
import time
from collections.abc import Callable
from typing import Any

from app.tools.terminal_runtime.terminal_input import keys_for_platform
from app.tools.terminal_runtime.terminal_session_service import TerminalSessionService

ALL_KEYS = sorted(set(keys_for_platform("Linux")) | set(keys_for_platform("Windows")))


class FakeWorker:
    pid = 4321
    rows = 24
    columns = 80
    integration_ready = False

    def __init__(self) -> None:
        self.geometry = {"rows": self.rows, "columns": self.columns}
        self.capabilities = frozenset(
            {"input_keys", "signal_interrupt", "signal_suspend",
             "signal_eof_canonical"}
            | {f"key_{key}" for key in ALL_KEYS}
        )
        self.callback: Callable[[dict[str, Any]], None] | None = None
        self.writes: list[dict[str, Any]] = []
        self.signals: list[str] = []
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

    def signal(self, signal_name: str) -> None:
        self.signals.append(signal_name)

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
        self.emit({"type": "exit", "exit_code": code})


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
        "app.tools.terminal_runtime.terminal_session_service.ProcessTerminalWorker", construct
    )
    monkeypatch.setattr(
        "app.tools.terminal_runtime.terminal_session_service.ShellResolver.resolve",
        lambda shell, integration="auto", **_: _spec(shell, integration),
    )
    service = TerminalSessionService(on_task_sessions_changed=on_changed or (lambda _task: None))
    return service, workers


def _spec(shell: str, integration: str) -> Any:
    from app.tools.terminal_runtime.shell_resolver import ShellSpec

    kind = "zsh" if shell == "auto" else shell
    return ShellSpec(kind, f"/bin/{kind}", (f"/bin/{kind}",), integration)


def start(service: TerminalSessionService, tmp_path: Any, *, task_id: int = 1,
          created_by: str = "agent", shell: str = "auto") -> str:
    meta = service.start(
        task_id=task_id,
        workspace_root=str(tmp_path),
        shell=shell,
        created_by=created_by,  # type: ignore[arg-type]
        run_id=1 if created_by == "agent" else None,
    )
    return str(meta["session_id"])


def never() -> bool:
    return False


def run_in_thread(target: Callable[[], Any]) -> tuple[threading.Thread, dict[str, Any]]:
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["result"] = target()
        except BaseException as exc:  # noqa: BLE001 - 测试把异常带回主线程断言
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
