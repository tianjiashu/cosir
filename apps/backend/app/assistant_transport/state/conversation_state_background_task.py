"""后台看板的 wire 契约与投影：snapshot 顶层 ``background_tasks`` 与 ``foreground_waits``。

看板是对话的一个投影，不另存事实：已结束工作的记录在各自后台占位行上（``BackgroundWork``），随工具
part 进入快照工作副本（后端工作副本保存完整历史，见 ``snapshot_window``）；还在跑的工作来自进程内
登记表的活条目。所以看板就是「工作副本里带记录的工具 part + 活条目」的纯函数，不读库、不读墙钟，
隐藏调用与编辑撤回的轮次已在构造 part 时排除，这里不再重复过滤。

``foreground_waits`` 是另一种事实：正在前台等待、可转到后台的调用，只服务工具卡片的「转到后台」，
只存在于登记表。工具卡片的状态不读这里——卡片状态的事实源是占位行（part 的 ``status`` /
``background``）。
"""

from collections.abc import Mapping, Sequence
from typing import Literal

from typing_extensions import TypedDict

from app.assistant_transport.state.conversation_state_message import ConversationStateMessage
from app.assistant_transport.state.conversation_state_part import ConversationBackgroundWork
from app.models.message_metadata import BackgroundKind, BackgroundWork

# ``settling``：工作已不在登记表里，但占位行还没落定——结论已到、等下一个模型步并入，或撤销已出队、
# 结算还没写完。两者都马上会落到 ``completed`` / ``cancelled``，看板不猜结局，也不让条目消失。
BackgroundTaskStatus = Literal["running", "settling", "completed", "cancelled"]


class ConversationBackgroundTask(TypedDict):
    """一条后台工作；进行中（运行、收尾）的在前，按开始先后；之后是最近结束的若干条。"""

    toolCallId: str
    kind: BackgroundKind
    label: str
    status: BackgroundTaskStatus
    startedAt: str
    endedAt: str | None


BACKGROUND_TASK_KEYS = frozenset(ConversationBackgroundTask.__annotations__)
BACKGROUND_WORK_KEYS = frozenset(ConversationBackgroundWork.__annotations__)
_KINDS = frozenset({"terminal_command", "child_agent"})
_STATUSES = frozenset({"running", "settling", "completed", "cancelled"})
_ACTIVE = frozenset({"running", "settling"})

# 展示策略：全部进行中的工作 + 最近结束的这么多条；持久化的历史不删。
MAX_ENDED_PER_TASK = 20


def project_background_work(work: BackgroundWork) -> ConversationBackgroundWork:
    """后台工作记录的 wire 形状（camelCase）；工具 part 与看板活条目共用。"""

    return {
        "kind": work.kind,
        "label": work.label,
        "startedAt": work.started_at,
        "endedAt": work.ended_at,
    }


def project_background_tasks(
    messages: Sequence[ConversationStateMessage],
    live: Mapping[str, BackgroundWork],
) -> list[ConversationBackgroundTask]:
    """从工作副本的工具 part 与登记表活条目投影看板。

    活条目优先：并行批次里占位行要等整批结束才写入，这期间 part 上还没有记录，名字与开始时间取自
    登记表。每个活条目的调用都有 part（卡片在模型流式输出时就已建好），所以只遍历 part。
    """

    entries: list[ConversationBackgroundTask] = []
    for message in messages:
        for part in message["parts"]:
            if part["type"] != "tool-call":
                continue
            tool_call_id = part["toolCallId"]
            live_work = live.get(tool_call_id)
            if live_work is not None:
                work = project_background_work(live_work)
                status: BackgroundTaskStatus = "running"
            elif "background" in part:
                work = part["background"]
                status = (
                    "settling" if part["status"] == "pending"
                    else "completed" if part["status"] == "completed"
                    else "cancelled"
                )
            else:
                continue
            entries.append(
                ConversationBackgroundTask(
                    toolCallId=tool_call_id,
                    kind=work["kind"],
                    label=work["label"],
                    status=status,
                    startedAt=work["startedAt"],
                    endedAt=None if status in _ACTIVE else work["endedAt"],
                )
            )
    active = sorted((e for e in entries if e["status"] in _ACTIVE), key=lambda e: e["startedAt"])
    ended = sorted(
        (e for e in entries if e["status"] not in _ACTIVE), key=lambda e: e["endedAt"] or ""
    )
    return active + ended[-MAX_ENDED_PER_TASK:]


def validate_background_work(value: object) -> None:
    """精确校验工具 part 上的 ``background`` 工作记录。

    异常:
        ValueError: 不满足契约。
    """

    if not isinstance(value, dict) or set(value) != BACKGROUND_WORK_KEYS:
        raise ValueError("snapshot tool background record is invalid")
    if value["kind"] not in _KINDS:
        raise ValueError("snapshot tool background kind is invalid")
    for key in ("label", "startedAt"):
        if not isinstance(value[key], str):
            raise ValueError(f"snapshot tool background {key} must be a string")
    if value["endedAt"] is not None and not isinstance(value["endedAt"], str):
        raise ValueError("snapshot tool background endedAt must be a string or null")


def validate_background_tasks(value: object) -> None:
    """精确校验 ``background_tasks``：键集合与取值枚举。

    同一调用可能先作为进行中条目、后作为结束条目出现在不同时刻的列表里，但同一份列表里不会重复
    （一次调用只有一个 part），这里仍校验唯一，防止投影拼接出错。

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
