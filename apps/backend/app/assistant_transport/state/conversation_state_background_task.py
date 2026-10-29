"""延迟结果的 Transport 契约：snapshot 顶层 ``background_tasks`` 与 ``foreground_waits``。

事实源是进程内的延迟结果登记表 ``deferred_registry``（``core/tool/background``）：条目不持久化、
不跨后端重启，所以两者都不从 canonical 记录重建，只在 snapshot 物化与登记表变化通知时整表读取。

两个字段是两种事实，分开投影：``background_tasks`` 只装后台工作，服务看板；``foreground_waits`` 是
正在前台等待、可转到后台的调用，只服务工具卡片的「转到后台」按钮（前台等待只存在于登记表）。
工具卡片的状态不读这里——卡片状态的事实源是占位行（工具 part 的 ``status`` / ``background``）。
"""

from typing import Literal

from typing_extensions import TypedDict

from app.core.tool.background.deferred_registry import DeferredEntry


class ConversationBackgroundTask(TypedDict):
    """一条后台工作；运行中的在前（按登记先后），之后是最近结束的若干条。"""

    toolCallId: str
    kind: Literal["terminal_command", "child_agent"]
    label: str
    status: Literal["running", "completed", "cancelled"]
    startedAt: str
    endedAt: str | None


BACKGROUND_TASK_KEYS = frozenset(ConversationBackgroundTask.__annotations__)
_KINDS = frozenset({"terminal_command", "child_agent"})
_STATUSES = frozenset({"running", "completed", "cancelled"})


def project_background_task(view: DeferredEntry) -> ConversationBackgroundTask:
    """把登记表的条目快照转换为 wire 条目（camelCase）。"""

    return {
        "toolCallId": view.tool_call_id,
        "kind": view.kind,
        "label": view.label,
        "status": view.status,
        "startedAt": view.started_at,
        "endedAt": view.ended_at,
    }


def validate_background_tasks(value: object) -> None:
    """精确校验 ``background_tasks``：键集合与取值枚举。

    同一调用可能先作为运行中条目、后作为结束条目出现在不同时刻的列表里，但同一份列表里不会重复
    （登记表 pop-once），这里仍校验唯一，防止投影拼接出错。

    异常:
        ValueError: 任一条目不满足契约。
    """

    if not isinstance(value, list):
        raise ValueError("snapshot background_tasks must be an array")
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != BACKGROUND_TASK_KEYS:
            raise ValueError("snapshot background result entry is invalid")
        tool_call_id = entry["toolCallId"]
        if not isinstance(tool_call_id, str) or not tool_call_id:
            raise ValueError("background result toolCallId must be a non-empty string")
        if tool_call_id in seen:
            raise ValueError("snapshot contains duplicate background result toolCallId")
        seen.add(tool_call_id)
        for key in ("label", "startedAt"):
            if not isinstance(entry[key], str):
                raise ValueError(f"background result {key} must be a string")
        if entry["endedAt"] is not None and not isinstance(entry["endedAt"], str):
            raise ValueError("background result endedAt must be a string or null")
        if entry["kind"] not in _KINDS:
            raise ValueError("background result kind is invalid")
        if entry["status"] not in _STATUSES:
            raise ValueError("background result status is invalid")


def validate_foreground_waits(value: object) -> None:
    """精确校验 ``foreground_waits``：互不重复的非空 ``tool_call_id`` 列表。

    异常:
        ValueError: 不满足契约。
    """

    if not isinstance(value, list):
        raise ValueError("snapshot foreground_waits must be an array")
    if any(not isinstance(item, str) or not item for item in value):
        raise ValueError("foreground_waits entries must be non-empty strings")
    if len(set(value)) != len(value):
        raise ValueError("snapshot contains duplicate foreground_waits toolCallId")
