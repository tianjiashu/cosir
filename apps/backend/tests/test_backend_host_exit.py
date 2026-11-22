"""宿主停止或消失时，真实后端（``python -m app``）连同它拉起的进程一起退出。

后端按桌面宿主的方式启动（``electron/main/backend/process.ts``）：stdin 是一条从不写入的管道，注入
``CODING_AGENT_PARENT_PIPE=stdin``，数据根指到临时目录。经 HTTP 建一个交互终端，让后端拉起 Terminal Worker
与 shell，再验证两条退出路径：

- 宿主请求停止 / 宿主崩溃：stdin 关闭，后端经 ``parent_watch`` 优雅关闭（lifespan 收尾、bootstate 写
  ``stopped``），终端进程随之清理。
- 宿主强杀后端（只杀后端本身，模拟兜底强杀漏掉的情况）：Worker 读到 stdin EOF 自行收尾，不留孤儿。

两个平台都跑；Windows 上验证的是 ``raise_signal`` 唤起优雅关闭与 Job Object 清理。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from tests.http_test_support import TRUSTED_HEADERS
from tests.process_tree_support import descendants, kill, survivors
from tests.terminal_test_support import requires_terminal_worker, terminal_worker_path

pytestmark = requires_terminal_worker

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_READY_TIMEOUT = 90.0
_WORKER_NAME = "terminal-worker"


@dataclass
class _Backend:
    process: subprocess.Popen[bytes]
    base_url: str
    bootstate: Path
    console: Path

    def phase(self) -> str | None:
        try:
            return json.loads(self.bootstate.read_text(encoding="utf-8")).get("phase")
        except (OSError, ValueError):
            return None

    def console_tail(self) -> str:
        try:
            return self.console.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            return ""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[_Backend]:
    data_dir = tmp_path / "data"
    bootstate = data_dir / ".cosir" / "runtime" / "backend.bootstate.json"
    console = tmp_path / "backend-console.log"
    port = _free_port()
    with console.open("wb") as sink:
        process = subprocess.Popen(
            [sys.executable, "-m", "app"],
            cwd=_BACKEND_DIR,
            stdin=subprocess.PIPE,
            stdout=sink,
            stderr=subprocess.STDOUT,
            env={
                **os.environ,
                "CODING_AGENT_PORT": str(port),
                "CODING_AGENT_RELOAD": "false",
                "CODING_AGENT_BOOT_STATE_FILE": str(bootstate),
                "CODING_AGENT_DATA_DIR": str(data_dir),
                "CODING_AGENT_TERMINAL_WORKER": str(terminal_worker_path()),
                "CODING_AGENT_PARENT_PIPE": "stdin",
                "CODING_AGENT_HOST_TOKEN": "test-host-token",
            },
        )
    started = _Backend(process, f"http://127.0.0.1:{port}", bootstate, console)
    try:
        deadline = time.monotonic() + _READY_TIMEOUT
        while started.phase() != "ready":
            if process.poll() is not None or time.monotonic() > deadline:
                pytest.fail(
                    f"backend did not become ready (phase {started.phase()}):\n{started.console_tail()}"
                )
            time.sleep(0.2)
        yield started
    finally:
        if process.poll() is None:
            for pid in descendants(process.pid):
                kill(pid)
            process.kill()
            process.wait(10)


def _start_terminal(backend: _Backend, workspace: Path) -> dict[int, str]:
    """经 HTTP 建工作区、会话与一个交互终端；返回此时后端的全部后代（含 Worker 与 shell）。"""

    workspace.mkdir()
    with httpx.Client(base_url=backend.base_url, timeout=30, headers=TRUSTED_HEADERS) as client:
        created = client.post("/workspaces", json={"name": "ws", "root_path": str(workspace)})
        assert created.status_code == 200, created.text
        workspace_id = created.json()["workspace_id"]
        session = client.put(f"/workspaces/{workspace_id}/unstarted-session")
        assert session.status_code == 200, session.text
        session_id = session.json()["session_id"]
        terminal = client.post(f"/sessions/{session_id}/terminal/sessions", json={"shell": "auto"})
        assert terminal.status_code == 200, terminal.text
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        tree = descendants(backend.process.pid)
        workers = [pid for pid, name in tree.items() if _WORKER_NAME in name.lower()]
        # Worker 之下还要有 shell，才算终端真的起来了。
        if workers and any(descendants(pid) for pid in workers):
            return tree
        time.sleep(0.2)
    pytest.fail(f"terminal worker and shell did not start: {descendants(backend.process.pid)}")


def test_closing_stdin_shuts_the_backend_down_gracefully_with_its_processes(
    backend: _Backend, tmp_path: Path
) -> None:
    """目的：宿主关闭存活管道（主动停止或崩溃）后，后端优雅关闭并清掉终端进程。
    潜在缺陷：Windows 上 SIGINT 唤不醒主线程，后端要等 15 秒强制退出、跳过 lifespan 收尾；
    Worker 没被关闭，shell 成为孤儿。"""

    tree = _start_terminal(backend, tmp_path / "ws")
    started = time.monotonic()
    assert backend.process.stdin is not None
    backend.process.stdin.close()

    try:
        backend.process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        pytest.fail(f"backend did not exit after its stdin closed:\n{backend.console_tail()}")
    # 优雅关闭走完 lifespan 收尾才会写 stopped；强制退出兜底（15 秒）不写。
    assert backend.phase() == "stopped", backend.console_tail()
    assert (
        time.monotonic() - started < 15
    ), "graceful shutdown must not wait for the hard-exit fallback"
    assert survivors(list(tree), timeout=10) == [], f"processes left behind: {tree}"


def test_killing_only_the_backend_still_cleans_up_the_terminal(
    backend: _Backend, tmp_path: Path
) -> None:
    """目的：后端被强杀（只杀后端本身）时，Worker 读到 stdin EOF 自行结束 shell 并退出。
    潜在缺陷：Worker 只靠后端关闭它，强杀后 Worker 与 shell 都成为孤儿。"""

    tree = _start_terminal(backend, tmp_path / "ws")

    kill(backend.process.pid)
    backend.process.wait(timeout=10)

    assert survivors(list(tree), timeout=10) == [], f"processes left behind: {tree}"
