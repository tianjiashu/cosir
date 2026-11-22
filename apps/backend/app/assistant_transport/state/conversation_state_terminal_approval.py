"""等待用户裁决的原终端调用；不携带可跨重启执行的作业。"""

from typing_extensions import TypedDict

from app.assistant_transport.state.wire_shape import (
    as_shape,
    require_bool,
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
    network: bool
    writeRoots: list[str]
    justification: str


def validate_terminal_approvals(value: object) -> None:
    seen: set[str] = set()
    for item in require_list(value, "terminal approvals"):
        row = as_shape(item, ConversationTerminalApproval, "terminal approval")
        for field in ("id", "toolCallId", "command", "shell", "cwd", "justification"):
            require_str(row[field], f"terminal approval {field}")
        require_int(row["originSessionId"], "terminal approval origin", minimum=1)
        require_bool(row["network"], "terminal approval network")
        for root in require_list(row["writeRoots"], "terminal approval roots"):
            require_str(root, "terminal approval root")
        if not row["id"] or row["id"] in seen:
            raise ValueError("terminal approval ids must be unique and nonempty")
        seen.add(row["id"])
