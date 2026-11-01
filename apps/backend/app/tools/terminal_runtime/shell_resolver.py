"""跨平台 interactive shell 解析。

支持的 shell 清单与可执行文件定位走共用的 ``app.tools.shell.host_shells``；本模块只承载交互
终端自己的语义：``auto`` 指用户默认 shell、接受任意可执行文件、展示名与集成支持，以及 worker
启动 shell 的 argv。
"""

import os
import platform
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import get_args

from app.tools.shell.host_shells import (
    PosixShell,
    WindowsShell,
    available_shells,
    find_shell,
)
from app.tools.terminal_runtime.errors import ShellResolutionError
from app.utils.host_platform import is_windows


@dataclass(frozen=True)
class ShellSpec:
    """worker 启动 shell 所需的不可变描述。"""

    kind: str
    executable: str
    argv: tuple[str, ...]
    integration: str = "auto"


# 可按名称请求的 shell（两个平台的并集）；指向它们的可执行路径也按该 shell 处理。
_KNOWN_SHELLS = frozenset(get_args(WindowsShell)) | frozenset(get_args(PosixShell))
# 有集成脚本的 shell：命令边界、退出码与 cwd 可用；其余 shell 降级（只能 output_quiet / match）。
INTEGRATED_SHELLS = frozenset({"zsh", "bash", "powershell", "pwsh"})
_LABELS = {"powershell": "Windows PowerShell", "pwsh": "PowerShell 7", "cmd": "Command Prompt"}


class ShellResolver:
    """解析 Windows PowerShell 与 POSIX 用户 shell。

    本类只做 shell 选择到 ``ShellSpec`` 的转换，不启动进程、不读取数据库，也不承载危险命令审批。

    解析完全依赖入参与宿主环境，不持有任何实例状态，因此调用方直接用
    ``ShellResolver.resolve(...)``，不需要构造实例、也不存在可注入的解析策略。
    """

    @staticmethod
    def resolve(
        requested: str = "auto",
        *,
        integration: str = "auto",
        system: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> ShellSpec:
        """将 shell 选择转换为 worker 可执行的 argv。

        ``system`` / ``environ`` 显式传入时优先于宿主实况，供工具描述与测试固定宿主行为。
        """

        value = requested.strip().lower() if requested else "auto"
        current_system = platform.system() if system is None else system

        def locate(name: str) -> str:
            path = find_shell(name, system=current_system, environ=environ)
            if path is None:
                raise ShellResolutionError(f"shell executable not found: {name}")
            return path

        if value == "custom":
            raise ShellResolutionError("custom shell requires an executable path")
        if value != "auto" and value not in _KNOWN_SHELLS:
            path = locate(requested)
            # 指向已知 shell 的路径（如 /opt/homebrew/bin/bash）按该 shell 处理，集成随之可用。
            name = Path(path).stem.lower()
            kind = name if name in _KNOWN_SHELLS else "custom"
            return ShellSpec(kind=kind, executable=path, argv=(path,), integration=integration)

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

        if value == "auto":
            configured = (os.environ if environ is None else environ).get("SHELL", "").strip()
            if configured:
                executable = locate(configured)
                return ShellSpec(Path(executable).name, executable, (executable,), integration)
            value = "zsh" if current_system == "Darwin" else "bash"

        executable = locate(value)
        return ShellSpec(value, executable, (executable,), integration)

    @staticmethod
    def available(
        *, system: str | None = None, environ: Mapping[str, str] | None = None
    ) -> list[dict[str, object]]:
        """按宿主平台探测已安装的 shell；人与 Agent 看到同一份清单。

        返回 ``{kind, label, isDefault, integrationSupported}``（camelCase，直接进入
        snapshot / HTTP / 工具结果）。默认项为 ``shell=auto`` 的解析结果；``$SHELL`` 指向清单外
        的 shell 时也作为默认项列出。只做 PATH 探测，不启动进程。
        """

        current_system = platform.system() if system is None else system
        try:
            default_kind: str | None = ShellResolver.resolve(
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
