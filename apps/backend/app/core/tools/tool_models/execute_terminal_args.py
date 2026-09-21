"""execute_terminal 工具的参数契约（按宿主平台分两个独立模型）。

本模块只定义模型可下发、门禁可校验的参数模型，不含任何执行逻辑。工具名始终是
``execute_terminal``：平台差异只体现在参数模型上，不产生第二个工具。

模型划分：

- ``WindowsExecuteTerminalArgs``：Windows 宿主，``shell`` 只收 cmd.exe / PowerShell 系列，
  ``command`` / ``workdir`` 按 Windows 语法（cmd 变量、``C:\\`` 路径）说明。
- ``MacExecuteTerminalArgs``：macOS 宿主，``shell`` 只收 POSIX shell，``command`` /
  ``workdir`` 按 POSIX 语法（``$VAR``、``/Users/...`` 路径）说明。

两个类是各自独立的完整契约（各自声明全部四个字段），不共享基类：平台差异不仅是 ``shell``
的取值集合，还包括命令语法与路径语法的说明，写成两个自洽的类后，读任何一个类都能拿到该平台
完整的下发契约。把「另一平台才存在的 shell」暴露给模型只会诱导必然失败的调用（例如在 macOS
上请求 ``cmd``），因此在参数契约层就按平台收口。

``resolve_execute_terminal_args_model`` 是唯一的平台选择入口，平台名由调用方注入
（``ExecuteTerminalTool`` 持有的 ``platform.system()`` 结果），本模块自身不重复探测平台。
非 Windows 宿主（macOS，以及开发态的 Linux 等 POSIX 系统）共用
``MacExecuteTerminalArgs``：二者的命令执行语义相同（``/bin/sh`` 语义 + POSIX 路径语法）。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# shell 取值集合按平台声明：Windows 与 POSIX 的可执行文件互不存在，故不作成一个大枚举。
WindowsExecuteTerminalShell = Literal["auto", "cmd", "powershell", "pwsh"]
MacExecuteTerminalShell = Literal["auto", "sh", "bash", "zsh", "fish", "pwsh"]

# 两个平台取值的并集，供 handler 侧类型标注使用（同源派生，不另写一份字面量）。
ExecuteTerminalShell = WindowsExecuteTerminalShell | MacExecuteTerminalShell


class WindowsExecuteTerminalArgs(BaseModel):
    """Windows 宿主上的 execute_terminal 参数。

    与 :class:`MacExecuteTerminalArgs` 是两份互不继承的完整契约：平台差异不仅在 ``shell``
    的取值集合，还包括命令与路径语法的说明，各自自洽即可独立读懂。下列字段的 ``description``
    是这些差异的唯一落点。
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    shell: WindowsExecuteTerminalShell = Field(
        default="auto",
        description=(
            "Shell that interprets command. 'auto' runs it through the host default shell "
            "(cmd.exe on Windows), so use cmd syntax (dir, type, where, %VAR%, &&). Pick "
            "'powershell' for Windows PowerShell, or 'pwsh' when PowerShell 7 is installed, to "
            "use PowerShell syntax (Get-ChildItem, $env:VAR, ;) instead. The command must match "
            "the selected shell; cmd and PowerShell syntax cannot be mixed in one call."
        ),
    )
    command: str = Field(
        min_length=1,
        description=(
            "Single command line interpreted by the selected shell with Windows syntax: quoting, "
            "variable expansion, pipes, and redirection follow that shell, not this tool. It runs "
            "non-interactively with no stdin, so anything that waits for input or opens a pager "
            "hangs until the timeout expires."
        ),
    )
    timeout: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Command timeout in seconds. Defaults to 60 and is clamped to at most 110; on "
            "expiry the command's process tree is killed and the call is reported as failed."
        ),
    )
    workdir: str | None = Field(
        default=None,
        description=(
            "Directory the command runs in, using Windows path syntax such as C:\\repo\\src: "
            "either an absolute path inside the workspace, or a path relative to the workspace "
            "root, which is also the default. It must already exist, and paths that leave the "
            "workspace are rejected."
        ),
    )


class MacExecuteTerminalArgs(BaseModel):
    """macOS 宿主上的 execute_terminal 参数。

    与 :class:`WindowsExecuteTerminalArgs` 是两份互不继承的完整契约，字段说明按 POSIX 语义
    （``$VAR`` 变量、``/Users/...`` 路径）给出。
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    shell: MacExecuteTerminalShell = Field(
        default="auto",
        description=(
            "Shell that interprets command. 'auto' runs it with /bin/sh semantics instead of the "
            "interactive login zsh, so shell rc files, aliases, and prompt settings do not apply. "
            "Pick 'sh', 'bash', 'zsh', or 'fish' (or 'pwsh' when PowerShell 7 is installed) to "
            "use that shell's own syntax (bash [[ ]], zsh arrays, fish builtins); the command "
            "must match the selected shell."
        ),
    )
    command: str = Field(
        min_length=1,
        description=(
            "Single command line interpreted by the selected shell with POSIX syntax: quoting, "
            "$VAR expansion, pipes, and redirection follow that shell, not this tool. It runs "
            "non-interactively with no stdin, so anything that waits for input or opens a pager "
            "hangs until the timeout expires."
        ),
    )
    timeout: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Command timeout in seconds. Defaults to 60 and is clamped to at most 110; on "
            "expiry the command's process tree is killed and the call is reported as failed."
        ),
    )
    workdir: str | None = Field(
        default=None,
        description=(
            "Directory the command runs in, using POSIX path syntax such as /Users/me/repo/src: "
            "either an absolute path inside the workspace, or a path relative to the workspace "
            "root, which is also the default. It must already exist, and paths that leave the "
            "workspace are rejected."
        ),
    )


def is_windows_host(system_name: str) -> bool:
    """判断注入的平台名是否 Windows（大小写与首尾空白容错）。

    「是不是 Windows」在本仓有两个消费方：选参数模型（:func:`resolve_execute_terminal_args_model`）
    与探测本机 shell（``ExecuteTerminalTool._detect_available_shells``）。二者必须共用同一判定口径，
    否则同一个平台名可能同时选中 Windows 参数契约却按 POSIX 分支探测 shell，使模型可见 ``enum``
    出现本平台不存在的取值。

    参数:
        system_name: 宿主平台名，取 ``platform.system()`` 的形态（``"Windows"`` / ``"windows"`` /
            ``" windows "`` 等）。

    返回:
        是 Windows 时返回 True，其它一律 False。

    异常:
        无。

    副作用:
        无（纯函数，不读环境、不启动进程）。
    """

    return system_name.strip().lower() == "windows"


def resolve_execute_terminal_args_model(
    system_name: str,
) -> type[WindowsExecuteTerminalArgs] | type[MacExecuteTerminalArgs]:
    """按宿主平台返回 execute_terminal 应使用的参数模型。

    参数:
        system_name: 宿主平台名，取 ``platform.system()`` 的形态（``"Windows"`` /
            ``"Darwin"`` / ``"Linux"`` 等，大小写与首尾空白容错）；由调用方注入而非在本模块
            重新探测，便于测试给定平台。

    返回:
        Windows 宿主返回 :class:`WindowsExecuteTerminalArgs`；其它平台（macOS 及 Linux 等
        POSIX 宿主）返回 :class:`MacExecuteTerminalArgs`，二者命令执行语义一致。

    异常:
        无（未识别的平台名按非 Windows 处理，与 shell 检测的 POSIX 分支保持同一取向）。

    副作用:
        无（纯函数，不读环境、不启动进程）。
    """

    return WindowsExecuteTerminalArgs if is_windows_host(system_name) else MacExecuteTerminalArgs
