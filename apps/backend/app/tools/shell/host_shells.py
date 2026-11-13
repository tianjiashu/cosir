"""本机 shell 目录：支持哪些 shell、本机有哪个、在哪，是两类终端共用的唯一事实源。

各平台支持的 shell 在这里声明一次，声明顺序即展示顺序；``execute_terminal`` 的参数模型由它派生
``Literal``，交互终端的候选清单由它取值；「用户默认 shell」（:func:`user_default_shell`）也只在这里。
不决定 ``auto`` 指什么、是否接受自定义路径、怎样启动：这些是工具自己的语义（一次性命令的 ``auto``
是 ``sh -c`` / ``cmd /c``，交互终端的 ``auto`` 是用户默认 shell 并接受任意可执行文件），分别在执行
后端 ``terminal_runtime.command.local_backend`` 与 ``terminal_runtime.session.shell_resolver``；
两者都经 :func:`find_shell` 定位，所以探测出的可用清单与实际启动的程序同一口径。

定位规则的取舍：
- Windows 的 ``cmd`` 以 ``COMSPEC`` 为准、缺省才查 ``cmd.exe``：这是系统声明的命令解释器。
- POSIX 的 ``sh`` 固定为 ``/bin/sh``，不依赖 PATH 是否含 ``/bin``。
- 不解析符号链接：``sh`` / ``bash`` 等按 argv[0] 决定行为（``/bin/sh`` 指向 bash 时以 POSIX 模式
  运行），解析成真实文件会悄悄换掉用户选的 shell。
"""

import os
import shutil
from collections.abc import Mapping
from typing import Literal, get_args

from app.utils.host_platform import is_windows

# 声明为 Literal 而非元组：参数模型要嵌进自己的 Literal（mypy 不接受由变量生成的 Literal），
# 运行期清单经 get_args 取出。Windows 与 POSIX 的可执行文件互不存在，故分平台声明。
WindowsShell = Literal["cmd", "powershell", "pwsh"]
PosixShell = Literal["sh", "bash", "zsh", "fish", "pwsh"]

_POSIX_SH = "/bin/sh"


def supported_shells(system: str | None = None) -> tuple[str, ...]:
    """当前平台支持的 shell，按声明顺序。"""

    return get_args(WindowsShell if is_windows(system) else PosixShell)


def available_shells(
    *,
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """当前平台支持且本机已安装的 shell，按声明顺序；只查文件系统与 PATH，不启动进程。"""

    return tuple(
        shell
        for shell in supported_shells(system)
        if find_shell(shell, system=system, environ=environ) is not None
    )


def find_shell(
    name: str,
    *,
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """返回 shell 的可执行文件绝对路径，本机没有时返回 ``None``。

    ``name`` 是 shell 名（``cmd`` / ``powershell`` / ``pwsh`` / ``sh`` / ``zsh`` 等），也可以是
    可执行文件名或路径（交互终端允许用户指定任意 shell）。``None`` 是「未安装」的查询结果，
    不是错误：探测可用清单时直接据此过滤，要求必须存在的调用方自行转换为各自的错误。

    ``system`` / ``environ`` 显式传入时优先于宿主实况，供工具描述与测试固定宿主行为。只查文件系统
    与 PATH，不启动进程。
    """

    env = os.environ if environ is None else environ
    if is_windows(system):
        if name == "cmd":
            comspec = env.get("COMSPEC", "").strip()
            return comspec or _which("cmd.exe")
        if name in {"powershell", "pwsh"}:
            return _which(f"{name}.exe")
    elif name == "sh":
        return _POSIX_SH if os.path.isfile(_POSIX_SH) and os.access(_POSIX_SH, os.X_OK) else None
    return _which(name)


def user_default_shell(environ: Mapping[str, str] | None = None) -> str:
    """POSIX 用户的默认 shell：``$SHELL``，未设置时取账户数据库里的登录 shell，再没有用 ``/bin/sh``。

    交互终端的 ``auto`` 与启动期登录环境解析共用这一条，二者起的是同一个 shell。账户数据库优先于
    按平台猜测（zsh / bash）：它就是用户的真实登录 shell。只在 POSIX 上调用（``pwd`` 是 POSIX 专有）。
    """

    configured = (os.environ if environ is None else environ).get("SHELL", "").strip()
    if configured:
        return configured
    import pwd

    return pwd.getpwuid(os.getuid()).pw_shell or _POSIX_SH


def _which(name: str) -> str | None:
    found = shutil.which(name)
    return os.path.abspath(found) if found else None
