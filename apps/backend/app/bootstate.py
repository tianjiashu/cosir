"""后端启动状态文件写入器。

在 uvicorn 真正监听之前，后端进程可能在导入 / 构建运行时阶段崩溃，
此时 ``/health`` 永远不可用，桌面端 supervisor 缺少任何结构化失败信号，
只能傻等健康检查超时再去翻日志。

本模块用一个原子写的结构化 JSON 文件（boot state file）作为
"启动就绪 / 失败"契约：桌面端 supervisor 轮询该文件即可在进程崩溃的
瞬间拿到结构化失败原因，无需读日志、无需等超时。

阶段取值：``booting``（启动中）、``ready``（应用装配成功）、
``failed``（启动失败）、``stopped``（优雅关闭）。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

BOOT_PHASE_BOOTING = "booting"
BOOT_PHASE_READY = "ready"
BOOT_PHASE_FAILED = "failed"
BOOT_PHASE_STOPPED = "stopped"

# 需要脱敏的敏感键名（大小写不敏感），与桌面端 supervisor 脱敏规则保持一致。
_SENSITIVE_KEYS = (
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "access_key",
    "accesskey",
    "private_key",
    "privatekey",
    "credential",
    "authorization",
)
# ``sk-`` 前缀密钥（DeepSeek / OpenAI 等），大小写不敏感。
_SK_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]*", re.IGNORECASE)
# 敏感键赋值：``key="value with spaces"`` / ``key: 'value'`` / ``key=token``。
# 引号感知：双引号值 (group3)、单引号值 (group4) 整体脱敏（允许含空格）。
# 闭合引号为可选（``"?`` / ``'?``）：未闭合引号（如 ``password="oops``）时
# ``[^"]*`` 会消费到下一个引号或文本末尾，避免残留明文（与 Rust 端行为一致）。
# 无引号值 (group5) 按单 token 截断（到空白/边界为止）。
_ASSIGN_PATTERN = re.compile(
    r"(?i)("
    + "|".join(re.escape(k) for k in _SENSITIVE_KEYS)
    + r")(\s*[:=]\s*)"
    + r"(?:\"([^\"]*)\"?|'([^']*)'?|([^\s\"',;)\]}]*))"
)


def _redact_assignment(match: re.Match[str]) -> str:
    """构造敏感键赋值的脱敏替换文本，保留原始引号形态。

    参数:
        match: ``_ASSIGN_PATTERN`` 的匹配对象。

    返回:
        键名 + 分隔符 + ``[REDACTED]``（含原引号）的替换字符串。

    异常:
        无。

    副作用:
        无。
    """
    key, sep = match.group(1), match.group(2)
    if match.group(3) is not None:
        return f'{key}{sep}"[REDACTED]"'
    if match.group(4) is not None:
        return f"{key}{sep}'[REDACTED]'"
    return f"{key}{sep}[REDACTED]"


def _redact_sensitive(text: str | None) -> str | None:
    """对可能含敏感信息的自由文本（如 traceback / 错误消息）做脱敏。

    屏蔽两类高风险内容：``sk-`` 前缀 API Key，以及
    ``api_key/token/secret/password`` 等敏感键的赋值片段。与桌面端
    supervisor 的 ``redact_sensitive_text`` 规则保持一致，避免明文密钥落盘。
    引号内的值（可能含空格）会被整体脱敏，无引号值按单 token 脱敏。

    参数:
        text: 原始文本；为 ``None`` 时原样返回。

    返回:
        脱敏后的文本；``None`` 输入返回 ``None``；非敏感内容原样保留。

    异常:
        无。

    副作用:
        无。
    """
    if text is None:
        return None
    # 顺序很关键：先处理敏感键赋值，再处理裸 ``sk-`` 前缀密钥。
    # 若先注入 ``sk-[REDACTED]`` 占位符，赋值脱敏会把占位符中的 ``]`` 当作值边界，
    # 产生 ``[REDACTED]]`` 畸形输出。
    redacted = _ASSIGN_PATTERN.sub(_redact_assignment, text)
    redacted = _SK_PATTERN.sub("sk-[REDACTED]", redacted)
    return redacted


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
        phase: 当前启动阶段，取值见模块级 ``BOOT_PHASE_*`` 常量。
        step: 可选的子步骤名，例如 ``start`` / ``app_ready``。
        error_type: 失败时的异常类型名。
        error_message: 失败时的异常消息；落盘前自动脱敏。
        traceback_text: 失败时的完整 traceback 文本；落盘前自动脱敏。

    返回:
        无。

    异常:
        无；文件写入失败会记录到可排查的错误日志并兜底输出到标准错误，不影响主流程。

    副作用:
        原子覆盖写入 ``path`` 指向的 JSON 文件（每次写入使用独立临时文件再 ``os.replace``）；
        ``error_message`` / ``traceback`` 中的敏感信息（API Key、Token 等）在写入前被脱敏。
    """
    payload: dict[str, str | None] = {
        "phase": phase,
        "step": step,
        "error_type": error_type,
        # error_message / traceback 可能含明文密钥，落盘前必须脱敏，
        # 避免 storage/backend.bootstate.json 成为明文密钥载体。
        "error_message": _redact_sensitive(error_message),
        "traceback": _redact_sensitive(traceback_text),
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
