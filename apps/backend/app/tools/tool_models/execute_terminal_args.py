"""execute_terminal 工具的参数契约（按宿主平台分两个独立模型）。

本模块只定义模型可下发、门禁可校验的参数模型，不含任何执行逻辑。工具名始终是
``execute_terminal``：平台差异只体现在参数模型上，不产生第二个工具。

模型划分：

- ``WindowsExecuteTerminalArgs``：Windows 宿主，``shell`` 只收 cmd.exe / PowerShell 系列，
  ``command`` / ``workdir`` 按 Windows 语法（cmd 变量、``C:\\`` 路径）说明。
- ``MacExecuteTerminalArgs``：macOS 宿主，``shell`` 只收 POSIX shell，``command`` /
  ``workdir`` 按 POSIX 语法（``$VAR``、``/Users/...`` 路径）说明。

两个类是各自独立的完整契约（各自声明全部字段），不共享基类：平台差异不仅是 ``shell``
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

from app.core.tool.schemas.tool_deferred import DeferredCapability, background_field

# shell 取值集合按平台声明：Windows 与 POSIX 的可执行文件互不存在，故不作成一个大枚举。
WindowsExecuteTerminalShell = Literal["auto", "cmd", "powershell", "pwsh"]
MacExecuteTerminalShell = Literal["auto", "sh", "bash", "zsh", "fish", "pwsh"]

# 两个平台取值的并集，供 handler 侧类型标注使用（同源派生，不另写一份字面量）。
ExecuteTerminalShell = WindowsExecuteTerminalShell | MacExecuteTerminalShell

# 命令级超时：handler 按它缺省与钳制，模型可见描述按它插值，二者同源。命令在后端线程里跑、可转到
# 后台，超时由执行后端杀进程树，没有外层工具子进程的硬超时，所以上限可以覆盖长构建与测试。
DEFAULT_COMMAND_TIMEOUT_S = 300.0
MAX_COMMAND_TIMEOUT_S = 3600.0

# 前后台执行：默认前台，前台等到上限仍未结束就自动转后台（结论以 background_result 回投并挂起
# Agent）；模型可用 ``background`` 一开始就放到后台，用户可把前台等待转走。参数模型（``background``
# 字段描述）与 handler 都读它，所以放在契约模块。
EXECUTE_TERMINAL_DEFERRED = DeferredCapability(default_background=False, foreground_limit_s=60.0)

_TIMEOUT_DESCRIPTION = (
    "Seconds before the command's process tree is killed and the call fails. Defaults to "
    f"{DEFAULT_COMMAND_TIMEOUT_S:g}, capped at {MAX_COMMAND_TIMEOUT_S:g}. Set it to cover long "
    "builds or test suites."
)


class WindowsExecuteTerminalArgs(BaseModel):
    # 不写类 docstring：它会作为顶层 description 进入模型可见 schema。与 MacExecuteTerminalArgs
    # 是两份互不继承的完整契约，平台差异（shell 取值、命令与路径语法）只落在下列字段描述里。

    model_config = ConfigDict(strict=True, extra="forbid")

    shell: WindowsExecuteTerminalShell = Field(
        default="auto",
        description=(
            "Shell that interprets command. 'auto' uses cmd.exe syntax (dir, type, where, %VAR%, "
            "&&); 'powershell' (Windows PowerShell) and 'pwsh' (PowerShell 7) use PowerShell "
            "syntax (Get-ChildItem, $env:VAR, ;). Do not mix the two in one command."
        ),
    )
    command: str = Field(
        min_length=1,
        description=(
            "Command or multi-line script (e.g. several lines or a here-string), interpreted as "
            "a whole by the selected shell: quoting, expansion, pipes, and redirection follow it."
        ),
    )
    timeout: float | None = Field(default=None, gt=0, description=_TIMEOUT_DESCRIPTION)
    workdir: str | None = Field(
        default=None,
        description=(
            "Directory to run in, e.g. C:\\repo\\src or src: absolute inside the workspace, or "
            "relative to the workspace root (the default). It must already exist."
        ),
    )
    background: bool | None = background_field(EXECUTE_TERMINAL_DEFERRED)


class MacExecuteTerminalArgs(BaseModel):
    # 不写类 docstring（同上）；字段描述按 POSIX 语义给出，Linux 等 POSIX 宿主共用。

    model_config = ConfigDict(strict=True, extra="forbid")

    shell: MacExecuteTerminalShell = Field(
        default="auto",
        description=(
            "Shell that interprets command. 'auto' uses /bin/sh, not the interactive login zsh, "
            "so rc files, aliases, and their PATH additions (nvm, pyenv) do not apply. 'bash', "
            "'zsh', 'fish', or 'pwsh' enable that shell's own syntax (bash [[ ]], zsh arrays, "
            "fish builtins)."
        ),
    )
    command: str = Field(
        min_length=1,
        description=(
            "Command or multi-line script (e.g. several lines or a heredoc), interpreted as a "
            "whole by the selected shell: quoting, $VAR expansion, pipes, and redirection follow "
            "it."
        ),
    )
    timeout: float | None = Field(default=None, gt=0, description=_TIMEOUT_DESCRIPTION)
    workdir: str | None = Field(
        default=None,
        description=(
            "Directory to run in, e.g. /Users/me/repo/src or src: absolute inside the workspace, "
            "or relative to the workspace root (the default). It must already exist."
        ),
    )
    background: bool | None = background_field(EXECUTE_TERMINAL_DEFERRED)


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
