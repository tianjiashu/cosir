"""宿主存活检测：宿主关闭管道后，后端必须发起优雅退出。"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

from app.parent_watch import install_parent_watch_from_env

# 子进程靠 cwd 导入 ``app``：后端不是已安装的包，pytest 的 pythonpath 设置不会传给子进程。
_BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_not_installed_without_parent_pipe(monkeypatch) -> None:
    """绕过宿主直接运行后端时不启用检测，避免开发调试时 stdin 关闭就把后端带走。"""
    monkeypatch.delenv("CODING_AGENT_PARENT_PIPE", raising=False)
    assert install_parent_watch_from_env() is False


def test_stdin_eof_triggers_graceful_interrupt() -> None:
    """目的：宿主关闭 stdin 后，检测线程以 SIGINT 唤起服务的优雅关闭。
    潜在缺陷：只读不发信号会让后端在宿主消失后继续驻留，占着端口与 SQLite。"""
    script = textwrap.dedent(
        """
        import signal
        import time
        from app.parent_watch import install_parent_watch_from_env
        # 与 uvicorn 一样由服务自己安装 SIGINT 处理，不依赖继承来的信号设置。
        signal.signal(signal.SIGINT, signal.default_int_handler)
        try:
            # stdin 已关闭，检测线程一启动就会发信号，所以安装也要在 try 里。
            assert install_parent_watch_from_env()
            time.sleep(10)
        except KeyboardInterrupt:
            print("graceful-interrupt", flush=True)
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=_BACKEND_DIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "CODING_AGENT_PARENT_PIPE": "stdin"},
    )
    assert child.stdin is not None and child.stdout is not None
    child.stdin.close()
    try:
        child.wait(timeout=8)
    except subprocess.TimeoutExpired:
        child.kill()
        raise AssertionError(child.stderr.read().decode()[-2000:])
    assert b"graceful-interrupt" in child.stdout.read()
