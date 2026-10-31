"""宿主存活检测：桌面宿主消失时后端自行退出。

桌面宿主（Electron main 进程）以一条管道作为后端的 stdin，从不写入，并注入
``CODING_AGENT_PARENT_PIPE=stdin``。宿主主动停止后端时关闭这条管道；宿主崩溃或被强杀时由操作系统
关闭它。这里读到 EOF 即发起退出——两种情况走同一条路径，同时覆盖 macOS 与 Windows，后端不会被遗留成
占着端口与 SQLite 的孤儿。

退出走 uvicorn 自己的信号处理（SIGINT 在两个平台都触发优雅关闭，lifespan 照常收尾）；若优雅关闭
在 ``_HARD_EXIT_AFTER_SECONDS`` 内没有完成，最后强制结束进程，保证宿主消失后后端一定会退出。
EOF 若在服务启动完成之前到达，uvicorn 启动后看到退出标志会直接返回、跳过 lifespan 关闭，这一窗口
只靠强制结束兜底（宿主主动停止时由宿主的强杀先一步收尾）。
绕过宿主直接运行后端（开发调试）时不设该变量，不启用检测。
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time

from app.config.logging.logger import log

_HARD_EXIT_AFTER_SECONDS = 15.0


def install_parent_watch_from_env() -> bool:
    """按环境变量决定是否启用宿主存活检测；启用时返回 True。

    在 uvicorn 安装信号处理之前调用即可：SIGINT 在 uvicorn 接管前到达时由 Python 默认处理抛出
    ``KeyboardInterrupt``，同样结束进程。
    """
    if os.environ.get("CODING_AGENT_PARENT_PIPE") != "stdin":
        return False
    threading.Thread(target=_watch_stdin, name="parent-host-watch", daemon=True).start()
    return True


def _watch_stdin() -> None:
    try:
        while sys.stdin.buffer.read(65536):
            # 宿主从不写入；读到内容只说明有人误用了 stdin，忽略并继续等 EOF。
            pass
    except (OSError, ValueError):
        # stdin 已不可读（被关闭或句柄失效）与 EOF 同义：宿主已经不在。
        pass
    log.warning(
        "parent_host_lost",
        extra={
            "msg": "宿主存活管道已关闭（宿主请求停止或已退出），后端开始自行关闭",
            "data": {"hard_exit_after_seconds": _HARD_EXIT_AFTER_SECONDS},
        },
    )
    _interrupt_main_thread()
    time.sleep(_HARD_EXIT_AFTER_SECONDS)
    log.error("parent_host_lost_hard_exit", extra={"msg": "优雅关闭超时，强制结束后端进程", "data": {}})
    os._exit(1)


def _interrupt_main_thread() -> None:
    """把 SIGINT 投给主线程，让服务立即进入优雅关闭。

    POSIX 上 ``raise_signal`` 只投给调用线程（这里是检测线程），主线程阻塞在 IO 等待里时不会被唤醒，
    关闭会被推迟到它自己醒来；所以用 ``pthread_kill`` 直接投给主线程。Windows 的 SIGINT 处理会唤醒
    主线程的等待，``raise_signal`` 即可。
    """
    if sys.platform == "win32":
        signal.raise_signal(signal.SIGINT)
    else:
        main_thread_id = threading.main_thread().ident
        assert main_thread_id is not None
        signal.pthread_kill(main_thread_id, signal.SIGINT)
