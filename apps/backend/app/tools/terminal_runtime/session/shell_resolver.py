"""交互终端的 shell 解析：把用户选的 shell 变成 worker 会话模式的启动描述 ``ShellSpec``。

支持的 shell 清单、可执行文件定位与用户默认 shell 走共用的 ``app.tools.shell.host_shells``；本模块
只承载交互终端自己的语义：``auto`` 指用户默认 shell、接受任意可执行文件、展示名与集成支持，以及
启动 shell 的 argv。不启动进程。
"""

import platform
from collections.abc import Mapping
from pathlib import Path
from typing import get_args

from app.tools.shell.host_shells import (
    PosixShell,
    WindowsShell,
    available_shells,
    find_shell,
    user_default_shell,
)
from app.tools.terminal_runtime.errors import ShellResolutionError
from app.tools.terminal_runtime.worker_client import ShellSpec
from app.utils.host_platform import is_windows

# 可按名称请求的 shell（两个平台的并集）。
_KNOWN_SHELLS = frozenset(get_args(WindowsShell)) | frozenset(get_args(PosixShell))
# 有集成脚本的 shell：命令边界、退出码与 cwd 可用；其余 shell 降级（只能 output_quiet / match）。
# 须与 worker 侧 ``terminal-worker/src/shell_integration/mod.rs`` 认的清单一致。
INTEGRATED_SHELLS = frozenset({"zsh", "bash", "powershell", "pwsh"})
_LABELS = {"powershell": "Windows PowerShell", "pwsh": "PowerShell 7", "cmd": "Command Prompt"}


def resolve_session_shell(
    requested: str = "auto",
    *,
    integration: str = "auto",
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ShellSpec:
    """将 shell 选择转换为 worker 可执行的 argv。

    ``kind`` 一律取可执行文件名（去扩展名、小写）：指向已知 shell 的路径（如 ``/opt/homebrew/bin/bash``）
    按该 shell 处理、集成随之可用；``auto`` 解析出的默认 shell 与按同名再次请求得到同一个 ``kind``，
    清单里的默认项与建出的会话一致。``system`` / ``environ`` 显式传入时优先于宿主实况，供测试固定宿主。

    异常:
        ShellResolutionError: 可执行文件不存在，或平台不支持所选 shell。
    """

    value = requested.strip().lower() if requested else "auto"
    current_system = platform.system() if system is None else system

    def locate(name: str) -> str:
        path = find_shell(name, system=current_system, environ=environ)
        if path is None:
            raise ShellResolutionError(f"shell executable not found: {name}")
        return path

    if value != "auto" and value not in _KNOWN_SHELLS:
        path = locate(requested)
        return ShellSpec(_kind_of(path), path, (path,), integration)

    if is_windows(current_system):
        if value == "cmd":
            executable = locate("cmd")
            return ShellSpec("cmd", executable, (executable, "/Q", "/D"), integration)
        if value in {"auto", "powershell"}:
            executable = locate("powershell")
            return ShellSpec("powershell", executable, (executable, "-NoLogo"), integration)
        if value == "pwsh":
            executable = locate("pwsh")
            return ShellSpec("pwsh", executable, (executable, "-NoLogo"), integration)
        raise ShellResolutionError(f"shell '{requested}' is not available on Windows")

    executable = locate(user_default_shell(environ) if value == "auto" else value)
    return ShellSpec(_kind_of(executable), executable, (executable,), integration)


def available_session_shells(
    *, system: str | None = None, environ: Mapping[str, str] | None = None
) -> list[dict[str, object]]:
    """按宿主平台探测已安装的 shell，供终端面板的新建菜单（``GET /terminal/shells``）。

    返回 ``{kind, label, isDefault, integrationSupported}``（camelCase，直接进入 HTTP 响应）。默认项为
    ``auto`` 的解析结果；默认 shell 不在支持清单里（如 tcsh）时也作为默认项列在最前。只做 PATH 探测，
    不启动进程。
    """

    current_system = platform.system() if system is None else system
    try:
        default_kind: str | None = resolve_session_shell(
            "auto", system=current_system, environ=environ
        ).kind
    except ShellResolutionError:
        default_kind = None
    kinds = list(available_shells(system=current_system, environ=environ))
    if default_kind is not None and default_kind not in kinds:
        kinds.insert(0, default_kind)
    return [
        {
            "kind": kind,
            "label": _LABELS.get(kind, kind),
            "isDefault": kind == default_kind,
            "integrationSupported": kind in INTEGRATED_SHELLS,
        }
        for kind in kinds
    ]


def _kind_of(executable: str) -> str:
    return Path(executable).stem.lower()
