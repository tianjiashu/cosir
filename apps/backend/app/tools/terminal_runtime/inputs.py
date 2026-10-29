"""Agent 向终端投递输入与等待条件的参数边界契约。

输入不声明自己是 shell 命令：``line`` 是一整行文字加一次 Enter，``text`` 是不带 Enter 的可打印
字符（全屏程序的单键命令、``y/n`` 单键确认：多一个 Enter 会落到下一个读输入的程序上），``keys``
是按键序列，``signal`` 是运行时信号。把输入权交给用户是独立的 HITL 工具 ``terminal_handoff``。
参数模型在工具边界校验宿主平台的键集合；会话实际能力另由 worker 握手声明，runtime 在投递前复查
（``require_input_capability``）。
"""

from __future__ import annotations

import platform
import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config.constant import Constant
from app.tools.terminal_runtime.errors import TerminalSessionError, TerminalSessionStateError

_NAVIGATION_KEYS: tuple[str, ...] = (
    "up",
    "down",
    "left",
    "right",
    "home",
    "end",
    "page_up",
    "page_down",
    "delete",
    "tab",
    "shift_tab",
    "escape",
    "backspace",
    "enter",
)


def keys_for_platform(system: str) -> tuple[str, ...]:
    """宿主键集合：控制键按平台区分（Windows 用 ctrl_z 代替 ctrl_d），导航键三平台一致。"""
    control = ("ctrl_c", "ctrl_z") if system == "Windows" else ("ctrl_c", "ctrl_d")
    return control + _NAVIGATION_KEYS


def signals_for_platform(system: str) -> tuple[str, ...]:
    return ("interrupt",) if system == "Windows" else ("interrupt", "suspend", "eof")


def _host_keys_schema(schema: dict[str, Any]) -> None:
    schema["enum"] = list(keys_for_platform(platform.system()))


def _host_signals_schema(schema: dict[str, Any]) -> None:
    schema["enum"] = list(signals_for_platform(platform.system()))


def _reject_control_characters(value: str, *, reserved_bytes: int) -> None:
    """line / text 共用：只检查可打印与字节预算，不解析接收程序的语法；worker 另行复查。"""
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        raise ValueError("text must not contain CR/LF or terminal control characters")
    if len(value.encode("utf-8")) + reserved_bytes > Constant.Terminal.MAX_AGENT_INPUT_BYTES:
        raise ValueError("terminal input exceeds 64 KiB")


class InputLine(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    type: Literal["line"]
    text: str = Field(
        description="One line without CR/LF or control characters, sent with one Enter; "
        "whitespace is kept. Empty just presses Enter."
    )

    @field_validator("text")
    @classmethod
    def validate_line(cls, value: str) -> str:
        _reject_control_characters(value, reserved_bytes=1)
        return value


class InputText(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    type: Literal["text"]
    text: str = Field(
        min_length=1,
        description="Characters typed without Enter, e.g. 'q' to quit less, 'y' at a "
        "single-key prompt, ':wq' in vim (then keys [\"enter\"]).",
    )

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        _reject_control_characters(value, reserved_bytes=0)
        return value


class InputKeys(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    type: Literal["keys"]
    keys: list[Annotated[str, Field(json_schema_extra=_host_keys_schema)]] = Field(
        description='1-32 keys sent in order, e.g. ["down", "enter"]; Enter is sent '
        "only if listed."
    )

    @field_validator("keys")
    @classmethod
    def validate_host_keys(cls, value: list[str]) -> list[str]:
        """宿主不支持的键在参数边界拒绝，不依赖会话存在或 worker 可用。"""
        if not 1 <= len(value) <= 32:
            raise ValueError("keys must contain 1 to 32 key names")
        system = platform.system()
        allowed = keys_for_platform(system)
        unknown = [key for key in value if key not in allowed]
        if unknown:
            raise ValueError(f"keys {unknown!r} are not supported on {system}")
        return value


class InputSignal(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    type: Literal["signal"]
    signal: str = Field(
        json_schema_extra=_host_signals_schema,
        description="interrupt is like Ctrl-C, suspend like Ctrl-Z, eof like Ctrl-D at an "
        "empty line. Delivery does not mean the program exited.",
    )

    @field_validator("signal")
    @classmethod
    def validate_host_signal(cls, value: str) -> str:
        system = platform.system()
        if value not in signals_for_platform(system):
            raise ValueError(f"signal {value!r} is not supported on {system}")
        return value


TerminalInput = InputLine | InputText | InputKeys | InputSignal

WaitUntil = Literal["command_returned", "output_quiet", "match", "elapsed"]


# 结束条件 + 总超时。等待只在前台、占着工具线程进行，所以总超时有上限；更长的工作交给
# execute_terminal 的后台执行。类 docstring 会进入模型 schema，因此写成模型可读的英文说明。
class TerminalWait(BaseModel):
    """When to return: one end condition plus an overall timeout."""

    model_config = ConfigDict(strict=True, extra="forbid")
    until: WaitUntil = Field(
        description="command_returned: the shell reports the command returned (needs shell "
        "integration). output_quiet: no output for quiet_ms. match: pattern matches a line from "
        "the cursor line on, or the redrawn screen in a full-screen app. elapsed: wait "
        "timeout_ms."
    )
    pattern: str | None = Field(
        default=None,
        max_length=512,
        description="Python regex for until=match; null otherwise.",
    )
    quiet_ms: int | None = Field(
        default=None,
        ge=100,
        le=60_000,
        description="Silence for until=output_quiet "
        f"(default {Constant.Terminal.WAIT_DEFAULT_QUIET_MS}); null otherwise.",
    )
    timeout_ms: int | None = Field(
        default=None,
        ge=0,
        le=Constant.Terminal.WAIT_MAX_TIMEOUT_MS,
        description=f"Overall limit (default {Constant.Terminal.WAIT_DEFAULT_TIMEOUT_MS}). For "
        "long builds or test suites, use execute_terminal with background=true instead.",
    )

    @model_validator(mode="after")
    def validate_condition(self) -> TerminalWait:
        if self.until == "match":
            if not self.pattern:
                raise ValueError("until=match requires pattern")
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"invalid pattern: {exc}") from exc
        return self

    @property
    def effective_timeout_ms(self) -> int:
        if self.timeout_ms is None:
            return Constant.Terminal.WAIT_DEFAULT_TIMEOUT_MS
        return self.timeout_ms

    @property
    def effective_quiet_ms(self) -> int:
        return self.quiet_ms or Constant.Terminal.WAIT_DEFAULT_QUIET_MS


# write 不带 wait 时的默认条件；静默时长与总超时取 ``effective_*`` 的默认值。
DEFAULT_WRITE_WAIT = TerminalWait(until="output_quiet")


def require_input_capability(input: Mapping[str, Any], capabilities: frozenset[str]) -> None:
    """参数边界已按宿主校验；投递前按本会话 worker 握手声明的能力复查。"""
    kind = input["type"]
    if kind == "keys":
        missing = [key for key in input["keys"] if f"key_{key}" not in capabilities]
        if "input_keys" not in capabilities or missing:
            raise TerminalSessionError(
                f"keys are not supported by this terminal worker: {missing or input['keys']}"
            )
    elif kind == "signal":
        required = {
            "interrupt": "signal_interrupt",
            "eof": "signal_eof_canonical",
            "suspend": "signal_suspend",
        }[str(input["signal"])]
        if required not in capabilities:
            raise TerminalSessionStateError(
                f"terminal signal is unsupported by worker: {input['signal']}"
            )


def summarize_input(input: Mapping[str, Any]) -> str:
    """动作行摘要：行文本、按键名或信号名；不承载输出。"""
    kind = input["type"]
    if kind in {"line", "text"}:
        return str(input["text"])
    if kind == "keys":
        return " ".join(input["keys"])
    return str(input["signal"])
