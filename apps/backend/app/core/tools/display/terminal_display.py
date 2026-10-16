"""终端工具的 UI 展示数据构造。"""

from pathlib import Path
from typing import Any


def build_terminal_display_data(
    *,
    command: str,
    workdir: Path,
    output: str,
    exit_code: int,
    timed_out: bool,
) -> dict[str, Any]:
    """构造完整的终端展示数据。

    命令和输出均按调用方提供的原文展示；输出保留 ANSI 控制序列。
    本函数不执行命令、不截断输出，也不负责工具状态判断。
    """

    return {
        "kind": "terminal-result",
        "command": command,
        "workdir": str(workdir),
        "output": output,
        "exit_code": exit_code,
        "timed_out": timed_out,
    }


def build_session_display_payload(
    payload: dict[str, object],
    *,
    include_terminal_info: bool = False,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """从 session 快照中保留允许进入工具展示的元数据。

    service 快照还含 worker、workspace、可执行文件和时间戳等诊断字段；它们属于后端事实，
    不进入 Assistant Transport。``extra`` 仅接收调用方已校验的操作元数据，不承载终端输入或输出。
    """

    fields = (
        "session_id",
        "status",
        "generation",
        "first_available_seq",
        "exit_code",
        "end_reason",
    )
    if include_terminal_info:
        fields += ("initial_cwd", "shell_kind", "label")
    result = {key: payload[key] for key in fields if key in payload}
    if extra:
        result.update(extra)
    return result


def build_terminal_session_display_data(
    payload: dict[str, object],
    *,
    include_terminal_info: bool = False,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """构造完整的交互式终端 session 展示 payload。"""

    return {
        "kind": "terminal-session",
        **build_session_display_payload(
            payload,
            include_terminal_info=include_terminal_info,
            extra=extra,
        ),
    }


def build_terminal_session_list_display_data(session_count: int) -> dict[str, object]:
    """只展示本次发现数量；会话实时状态从 service 获取，不复制到列表卡片。"""

    return {"kind": "terminal-session", "session_count": session_count}


def build_terminal_observation_display_data(payload: dict[str, Any]) -> dict[str, object]:
    """从本次观察投影 UI 元数据，不在输入接受后再次查询可能已退役的会话。"""
    state = payload["state"]
    return build_terminal_session_display_data(
        {
            "session_id": payload["session_id"],
            "generation": payload["generation"],
            "status": state["session_status"],
            "exit_code": state["session_exit_code"],
            "end_reason": state["session_end_reason"],
        }
    )
