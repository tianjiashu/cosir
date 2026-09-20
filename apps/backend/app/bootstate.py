"""后端启动状态文件写入器。

在 uvicorn 真正监听之前，后端进程可能在导入 / 构建运行时阶段崩溃，
此时 ``/health`` 永远不可用，桌面端 supervisor 缺少任何结构化失败信号，
只能傻等健康检查超时再去翻日志。

本模块用一个原子写的结构化 JSON 文件（boot state file）作为
"启动就绪 / 失败"契约：桌面端 supervisor 轮询该文件即可在进程崩溃的
瞬间拿到结构化失败原因，无需读日志、无需等超时。

阶段取值见 ``Constant.Boot`` 的四个稳定字符串：``booting``（启动中）、
``ready``（应用装配成功）、``failed``（启动失败）、``stopped``（优雅关闭）。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path


def write_bootstate(
    path: Path,
    phase: str,
    *,
    step: str | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
    traceback_text: str | None = None,
) -> None:
    """原子写入后端启动状态文件。

    参数:
        path: 启动状态文件绝对路径。
        phase: 当前启动阶段，取值见 ``Constant.Boot`` 的四个稳定字符串。
        step: 可选的子步骤名，例如 ``start`` / ``app_ready``。
        error_type: 失败时的异常类型名。
        error_message: 失败时的异常消息，按原文写入。
        traceback_text: 失败时的完整 traceback 文本，按原文写入。

    返回:
        无。

    异常:
        无；文件写入失败会记录到可排查的错误日志并兜底输出到标准错误，不影响主流程。

    副作用:
        原子覆盖写入 ``path`` 指向的 JSON 文件（每次写入使用独立临时文件再 ``os.replace``）。
    """
    payload: dict[str, str | None] = {
        "phase": phase,
        "step": step,
        "error_type": error_type,
        "error_message": error_message,
        "traceback": traceback_text,
    }
    # 移除 None 值，保持文件整洁且便于前端解析。
    payload = {key: value for key, value in payload.items() if value is not None}

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 每次写入使用独立临时文件，避免多线程/多进程并发写共享同名临时文件时
        # ``os.replace`` 源文件被并发删除导致写入静默失败。
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f"{path.stem}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            # 进程内加锁，进一步保证最终文件原子可见、避免写入撕裂。
            with _write_lock:
                os.replace(tmp_name, path)
        finally:
            # ``os.replace`` 成功后临时文件已被移除；异常残留时清理，避免遗留垃圾文件。
            if os.path.exists(tmp_name):
                with contextlib.suppress(OSError):
                    os.remove(tmp_name)
    except OSError as exc:
        # 启动状态文件写入失败不应阻断主流程，但必须留下可排查的本地记录。
        _record_write_failure(path, exc)


# 进程内写锁：避免多线程并发写同一启动状态文件时出现临时文件被并发替换删除
# 或写入撕裂（即便临时文件名唯一，加锁可进一步保证最终文件原子可见）。
_write_lock = threading.Lock()


def _record_write_failure(path: Path, exc: OSError) -> None:
    """记录启动状态文件写入失败到可排查的落盘日志，并兜底输出到标准错误。

    参数:
        path: 启动状态文件绝对路径。
        exc: 写入过程中捕获的 ``OSError``。

    返回:
        无。

    异常:
        无；记录失败本身被静默忽略，不影响主流程。

    副作用:
        向 ``<启动状态文件父目录>/bootstate.error.log`` 追加一行结构化错误，
        并输出到标准错误作为最后兜底。
    """
    message = (
        f"[{datetime.now().isoformat(timespec='seconds')}] "
        f"[bootstate] 写入启动状态文件失败 {path}: {exc}"
    )
    # 落盘到与启动状态文件同目录的错误日志，保证有可排查的本地记录（规范第六章）。
    try:
        error_log = path.parent / "bootstate.error.log"
        with open(error_log, "a", encoding="utf-8") as handle:
            handle.write(message + "\n")
    except OSError:
        pass
    print(message, file=sys.stderr)


def boot_state_file_from_env() -> Path | None:
    """从环境变量解析启动状态文件路径；未设置时返回 ``None``。

    参数:
        无。

    返回:
        环境变量 ``CODING_AGENT_BOOT_STATE_FILE`` 指向的绝对路径；未设置时返回 ``None``。

    异常:
        无。

    副作用:
        无。
    """
    raw = os.environ.get("CODING_AGENT_BOOT_STATE_FILE")
    if not raw:
        return None
    return Path(raw)
