"""跨平台 interactive shell 解析。"""

import os
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.tools.terminal_runtime.errors import TerminalSessionError


@dataclass(frozen=True)
class ShellSpec:
    """worker 启动 shell 所需的不可变描述。"""

    kind: str
    executable: str
    argv: tuple[str, ...]
    integration: str = "auto"


# 有集成脚本的 shell：命令边界、退出码与 cwd 可用；其余 shell 降级（只能 output_quiet / match）。
_KNOWN_SHELLS = frozenset({"bash", "zsh", "fish", "pwsh", "powershell", "cmd"})
INTEGRATED_SHELLS = frozenset({"zsh", "bash", "powershell", "pwsh"})
_CANDIDATES: dict[str, tuple[str, ...]] = {
    "Windows": ("powershell", "pwsh", "cmd"),
    "Darwin": ("zsh", "bash", "fish", "pwsh"),
    "Linux": ("bash", "zsh", "fish", "pwsh"),
}
_EXECUTABLES = {"powershell": "powershell.exe", "cmd": "cmd.exe"}
_LABELS = {"powershell": "Windows PowerShell", "pwsh": "PowerShell 7", "cmd": "Command Prompt"}


class ShellResolutionError(TerminalSessionError):
    """shell 参数无法解析。"""

    code = "TERMINAL_SHELL_UNAVAILABLE"


class ShellResolver:
    """解析 Windows PowerShell 与 POSIX 用户 shell。

    本类只做可执行文件解析，不启动进程、不读取数据库，也不承载危险命令审批。

    解析完全依赖入参与宿主环境，不持有任何实例状态，因此调用方直接用
    ``ShellResolver.resolve(...)``，不需要构造实例、也不存在可注入的解析策略。
    """

    @staticmethod
    def resolve(
        requested: str = "auto",
        *,
        integration: str = "auto",
        system: str | None = None,
        environ: dict[str, str] | None = None,
    ) -> ShellSpec:
        """将 shell 选择转换为 worker 可执行的 argv。

        ``system`` / ``environ`` 显式传入时优先于宿主实况，供工具描述与测试固定宿主行为。
        """

        value = requested.strip().lower() if requested else "auto"
        current_system = system or platform.system()
        env = environ if environ is not None else os.environ

        if value == "custom":
            raise ShellResolutionError("custom shell requires an executable path")
        if value not in {"auto", "cmd", "powershell", "pwsh", "bash", "zsh", "fish"}:
            path = ShellResolver._resolve_executable(requested)
            # 指向已知 shell 的路径（如 /opt/homebrew/bin/bash）按该 shell 处理，集成随之可用。
            name = Path(path).stem.lower()
            kind = name if name in _KNOWN_SHELLS else "custom"
            return ShellSpec(kind=kind, executable=path, argv=(path,), integration=integration)

        if current_system == "Windows":
            if value == "cmd":
                executable = ShellResolver._resolve_executable("cmd.exe")
                return ShellSpec("cmd", executable, (executable, "/Q", "/D"), integration)
            if value in {"auto", "powershell"}:
                executable = ShellResolver._resolve_executable("powershell.exe")
                return ShellSpec("powershell", executable, (executable, "-NoLogo"), integration)
            if value == "pwsh":
                executable = ShellResolver._resolve_executable("pwsh.exe")
                return ShellSpec("pwsh", executable, (executable, "-NoLogo"), integration)
            raise ShellResolutionError(f"shell '{requested}' is not available on Windows")

        if value == "auto":
            configured = env.get("SHELL", "").strip()
            if configured:
                executable = ShellResolver._resolve_executable(configured)
                return ShellSpec(Path(executable).name, executable, (executable,), integration)
            value = "zsh" if current_system == "Darwin" else "bash"

        executable = ShellResolver._resolve_executable(value)
        return ShellSpec(value, executable, (executable,), integration)

    @staticmethod
    def available(
        *, system: str | None = None, environ: dict[str, str] | None = None
    ) -> list[dict[str, object]]:
        """按宿主平台探测已安装的 shell；人与 Agent 看到同一份清单。

        返回 ``{kind, label, isDefault, integrationSupported}``（camelCase，直接进入
        snapshot / HTTP / 工具结果）。默认项为 ``shell=auto`` 的解析结果；``$SHELL`` 指向清单外
        的 shell 时也作为默认项列出。只做 PATH 探测，不启动进程。
        """

        current_system = system or platform.system()
        candidates = _CANDIDATES.get(current_system, _CANDIDATES["Linux"])
        try:
            default_kind: str | None = ShellResolver.resolve(
                "auto", system=current_system, environ=environ
            ).kind
        except ShellResolutionError:
            default_kind = None
        kinds = [kind for kind in candidates if shutil.which(_EXECUTABLES.get(kind, kind))]
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

    @staticmethod
    def _resolve_executable(value: str) -> str:
        candidate = shutil.which(value)
        if candidate is None:
            path = Path(value)
            if path.is_file() and os.access(path, os.X_OK):
                return str(path.resolve())
            raise ShellResolutionError(f"shell executable not found: {value}")
        return str(Path(candidate).resolve())
