"""等待用户裁决的原终端调用；不携带可跨重启执行的作业。"""

from typing_extensions import TypedDict

from app.assistant_transport.state.wire_shape import (
    as_shape,
    require_int,
    require_list,
    require_str,
)


class ConversationTerminalApproval(TypedDict):
    id: str
    originSessionId: int
    toolCallId: str
    command: str
    shell: str
    cwd: str
    intent: str


def validate_terminal_approvals(value: object) -> None:
    seen: set[str] = set()
    for item in require_list(value, "terminal approvals"):
        row = as_shape(item, ConversationTerminalApproval, "terminal approval")
        for field in ("id", "toolCallId", "command", "shell", "cwd", "intent"):
            require_str(row[field], f"terminal approval {field}")
        require_int(row["originSessionId"], "terminal approval origin", minimum=1)
        if not row["id"] or row["id"] in seen:
            raise ValueError("terminal approval ids must be unique and nonempty")
        seen.add(row["id"])
