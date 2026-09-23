"""Platform-aware model descriptions for the interactive terminal tools."""

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
    """Build the model-facing schema with host-specific terminal guidance.

    The Pydantic models provide the stable validation contract. This projection only
    enriches field descriptions for the current host, matching the dynamic schema
    pattern used by ``execute_terminal``; it does not change validation or add an
    enum that would incorrectly reject custom shell executables.
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
    """Return the current resolver result for ``shell=auto`` when available."""

    try:
        return ShellResolver().resolve("auto").kind
    except Exception:
        return ""


def _shell_parameter_description(system: str, resolved_kind: str) -> str:
    """Describe interactive shell choices using the current host contract."""

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
    """Describe the workspace-relative cwd using host path syntax."""

    syntax = "Windows path syntax" if system == "Windows" else "POSIX path syntax"
    return (
        f"Initial directory using {syntax}, relative to the workspace; defaults to the "
        "workspace root and must resolve to an existing directory inside it."
    )


def _signal_parameter_description(system: str) -> str:
    """Describe signal meanings and host capability limits."""

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
    """Return a concise description reflecting the current host shell contract.

    The tool execution path remains authoritative for capability checks. This provider only
    improves model guidance and must therefore remain safe to fail back to ``fallback``.
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
