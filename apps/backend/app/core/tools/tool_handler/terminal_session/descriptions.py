"""交互终端工具的宿主平台感知模型描述。

本模块只生产**模型可见文本**：工具描述与参数说明会按当前宿主平台补全（shell 解析结果、
cwd 路径语法、信号能力边界）。这些文本一律使用英文——它们是模型契约，不是注释。工具执行
路径仍然是能力检查的权威来源，这里只改善模型引导，因此必须允许失败回退。
"""

import platform
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from pydantic import BaseModel

from app.core.tools.schemas.tool_names import (
    TOOL_TERMINAL_SIGNAL,
    TOOL_TERMINAL_START,
    TOOL_TERMINAL_WRITE,
)
from app.service.terminal.shell_resolver import ShellResolver


def build_terminal_session_parameters_schema(
    tool_name: str,
    args_model: type[BaseModel],
) -> Mapping[str, Any]:
    """在 Pydantic 校验契约之上，为当前宿主补充模型可见的参数说明。

    Pydantic 模型提供稳定的校验契约；本投影只按宿主平台改写字段 ``description``（与
    ``execute_terminal`` 使用同一套动态 schema 模式），既不改动校验规则，也不添加会把自定义
    shell 可执行文件误判为非法的 enum。

    参数:
        tool_name: 工具名，决定补全哪些字段；目前只处理 ``terminal_start``（``shell``、
            ``cwd``）与 ``terminal_signal``（``signal``）。
        args_model: 该工具的 Pydantic 参数模型。

    返回:
        深拷贝并补全后的 JSON schema；``properties`` 结构异常时原样返回。

    异常:
        无；``ShellResolver`` 解析失败时回退为「宿主默认」措辞。

    副作用:
        无；入参模型不被修改（内部使用 ``deepcopy``）。
    """

    schema = deepcopy(args_model.model_json_schema())
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return schema

    system = platform.system()
    resolved_kind = _resolve_auto_shell_kind()
    if tool_name == TOOL_TERMINAL_START:
        shell = properties.get("shell")
        if isinstance(shell, dict):
            shell["description"] = _shell_parameter_description(system, resolved_kind)
        cwd = properties.get("cwd")
        if isinstance(cwd, dict):
            cwd["description"] = _cwd_parameter_description(system)
    elif tool_name == TOOL_TERMINAL_SIGNAL:
        signal = properties.get("signal")
        if isinstance(signal, dict):
            signal["description"] = _signal_parameter_description(system)

    return schema


def _resolve_auto_shell_kind() -> str:
    """返回 ``shell=auto`` 在当前宿主上解析出的 shell 种类。

    返回:
        解析成功时为 ``shell`` / ``cmd`` / ``powershell`` 等种类名；解析失败或宿主不支持时
        返回空字符串，由调用方降级为「宿主默认」措辞。

    异常:
        无；``ShellResolver`` 的任何异常都被吞掉，因为本函数只服务描述生成。

    副作用:
        无。
    """

    try:
        return ShellResolver().resolve("auto").kind
    except Exception:
        return ""


def _shell_parameter_description(system: str, resolved_kind: str) -> str:
    """按当前宿主契约生成 ``shell`` 参数的模型说明。

    参数:
        system: ``platform.system()`` 的结果，决定使用哪种宿主措辞。
        resolved_kind: ``shell=auto`` 的解析结果；为空时降级为「宿主默认」。

    返回:
        英文参数说明，包含 ``auto`` 的当前解析结果、可用取值与可执行文件要求。

    异常:
        无。

    副作用:
        无。
    """

    resolved = resolved_kind or "the host default"
    if system == "Windows":
        return (
            "Shell for the hidden Windows ConPTY session. 'auto' currently resolves to "
            f"{resolved}; use 'cmd', 'powershell', or 'pwsh' when installed, or provide "
            "a resolvable executable. Use Windows command syntax."
        )
    if system == "Darwin":
        return (
            "Shell for the hidden macOS PTY session. 'auto' uses $SHELL and falls back to "
            f"zsh (currently {resolved}); use POSIX shell syntax or a resolvable executable."
        )
    if system == "Linux":
        return (
            "Shell for the hidden Linux PTY session. 'auto' uses $SHELL and falls back to "
            f"bash (currently {resolved}); use POSIX shell syntax or a resolvable executable."
        )
    return (
        "Shell for the hidden local PTY session. 'auto' uses the host default "
        f"(currently {resolved}); use the host shell syntax or a resolvable executable."
    )


def _cwd_parameter_description(system: str) -> str:
    """按宿主路径语法生成 ``cwd`` 参数的模型说明。

    参数:
        system: ``platform.system()`` 的结果，决定使用 Windows 还是 POSIX 路径措辞。

    返回:
        英文参数说明：相对工作区根解析、默认工作区根，且必须解析为工作区内已存在的目录。

    异常:
        无。

    副作用:
        无。
    """

    syntax = "Windows path syntax" if system == "Windows" else "POSIX path syntax"
    return (
        f"Initial directory using {syntax}, relative to the workspace; defaults to the "
        "workspace root and must resolve to an existing directory inside it."
    )


def _signal_parameter_description(system: str) -> str:
    """生成 ``signal`` 参数的模型说明，并写明宿主能力限制。

    参数:
        system: ``platform.system()`` 的结果，决定是否提示当前平台不支持信号。

    返回:
        英文参数说明：三个信号的含义，以及该平台的能力边界（Windows 明确提示改用
        ``terminal_write``）。

    异常:
        无。

    副作用:
        无。
    """

    meaning = "interrupt is Ctrl-C-like, eof is canonical EOF, and suspend is Ctrl-Z-like"
    if system == "Windows":
        return (
            f"Signal to send: {meaning}. The current Windows worker does not advertise "
            "terminal signal support; use terminal_write for input instead."
        )
    return (
        f"Signal to send: {meaning}. Availability depends on the capabilities of the "
        "current PTY worker."
    )


def describe_terminal_tool(tool_name: str, fallback: str) -> str:
    """生成与当前宿主 shell 契约一致的模型可见工具描述。

    工具执行路径仍然是能力检查的权威来源；本函数只改善模型引导，因此必须允许安全回退到
    ``fallback``（handler 自己声明的基准描述）。

    参数:
        tool_name: 工具名，决定追加哪些宿主说明：``terminal_start`` 追加宿主与隐藏窗口说明、
            ``terminal_write`` 追加提交约定、``terminal_signal`` 追加能力边界。
        fallback: handler 声明的基准描述，作为返回文本的前缀。

    返回:
        英文工具描述：``fallback`` 加该工具的宿主补充说明；``tool_name`` 未匹配时只追加宿主
        说明。

    异常:
        无；``ShellResolver`` 失败时省略解析结果那一句。

    副作用:
        无。
    """

    system = platform.system()
    if system == "Windows":
        host = (
            "Windows uses cmd.exe for shell=cmd and PowerShell for shell=auto/powershell; "
            "pwsh requires an installed PowerShell 7 executable."
        )
        signal = (
            "The Windows worker currently advertises no terminal_signal capability; do not "
            "use terminal_signal for interrupt, EOF, or suspend. Use terminal_write for input."
        )
    elif system == "Darwin":
        host = "macOS resolves shell=auto from $SHELL, falling back to zsh."
        signal = (
            "Interrupt, canonical EOF, and suspend are available when supported by the PTY "
            "worker."
        )
    else:
        host = "Linux/Unix resolves shell=auto from $SHELL, falling back to bash."
        signal = (
            "Interrupt, canonical EOF, and suspend are available when supported by the PTY "
            "worker."
        )

    resolved_kind = _resolve_auto_shell_kind()
    if resolved_kind:
        host = f"{host} On this host shell=auto currently resolves to {resolved_kind}."

    hidden = (
        "The PTY worker runs without opening a visible local terminal window; the frontend "
        "only provides a read-only preview."
    )
    if tool_name == TOOL_TERMINAL_START:
        return f"{fallback} {host} {hidden}"
    if tool_name == TOOL_TERMINAL_WRITE:
        return (
            f"{fallback} {host} Send command text in data without decoding escape sequences; "
            "set submit=true to append one real Enter key (CR, 0x0D). This is required to "
            "reliably submit commands through Windows ConPTY."
        )
    if tool_name == TOOL_TERMINAL_SIGNAL:
        return f"{fallback} {signal}"
    return f"{fallback} {host}"
