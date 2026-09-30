"""系统提示词变更的 diff 构建与进程内广播。

本模块负责把已经通过配置校验的新旧文本转换为最小的 unified diff，并把通知投递给当前
进程中已经物化且符合作用域条件的 ``TaskRuntimeSpace``。它不负责写配置文件、不负责更新
Agent Registry，也不负责把通知持久化到 Conversation context。
"""

from __future__ import annotations

import difflib
import os

from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfileType
from app.task_runtime.system_prompt_delta import SystemPromptDelta
from app.task_runtime.system_prompt_delta_source import SystemPromptDeltaSource
from app.task_runtime.task_runtime_space import TaskRuntimeSpace
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


def build_system_prompt_delta(
    *,
    source: SystemPromptDeltaSource,
    previous: str,
    current: str,
    scope: str | None = None,
) -> SystemPromptDelta | None:
    """根据新旧提示词正文生成非空的系统提示词增量。

    参数:
        source: 发生变化的提示词层。
        previous: 原始层正文。
        current: 新的层正文。
        scope: 可选的 workspace 作用域。

    返回:
        文本发生变化时返回包含 unified diff 的 :class:`SystemPromptDelta`；正文相同返回 None，
        避免无意义广播。

    异常:
        无。输入文本按行处理，不执行模板渲染或文件 IO。

    副作用:
        无。
    """

    if previous == current:
        return None
    diff = "".join(
        difflib.unified_diff(
            previous.splitlines(keepends=True),
            current.splitlines(keepends=True),
            fromfile="previous",
            tofile="current",
            n=0,
        )
    ).strip()
    if not diff:
        return None
    return SystemPromptDelta(source=source, diff=diff, scope=scope)


def broadcast_system_prompt_delta(delta: SystemPromptDelta) -> int:
    """把系统提示词增量投递给当前进程中符合条件的 Task。

    参数:
        delta: 已由 :func:`build_system_prompt_delta` 生成并通过配置校验的增量。

    返回:
        实际入队的 Task 数量。未物化 Task、尚未创建 context manager 的 space，以及不属于
        目标 Agent/作用域的 space 不计入返回值。

    异常:
        ValueError: ``delta`` 的 diff 为空白时由 ``to_message`` 抛出。

    副作用:
        读取当前 registry 的 space 快照，并向符合条件的 space 队列投递一条临时通知；不创建
        新 space，不写数据库，不修改 Registry，不阻塞正在进行的模型请求。
    """

    spaces = task_runtime_spaces.existing_spaces()
    delivered = 0
    for space in spaces:
        if _should_notify_space(space, delta):
            space.defer_system_prompt_delta(delta)
            delivered += 1
    log.info(
        "system_prompt_delta_broadcast",
        extra={
            "msg": "系统提示词变更已广播到进程内 Task runtime space",
            "data": {
                "source": delta.source.value,
                "scope": delta.scope,
                "diff_length": len(delta.diff),
                "materialized_space_count": len(spaces),
                "delivered_count": delivered,
            },
        },
    )
    return delivered


def _should_notify_space(space: TaskRuntimeSpace, delta: SystemPromptDelta) -> bool:
    """按已装配的 context manager 判断一个 space 是否命中通知作用域。"""

    manager = space.get_context_manager()
    if manager is None:
        return False
    if (
        delta.source is SystemPromptDeltaSource.MAIN_AGENT_PROMPT
        and manager.agent_profile.agent_type is not AgentProfileType.MAIN
    ):
        return False
    if delta.source is SystemPromptDeltaSource.WORKSPACE_INSTRUCTIONS:
        if delta.scope is None:
            return False
        return _normalize_path(manager.workspace_root) == _normalize_path(delta.scope)
    return True


def _normalize_path(path: str) -> str:
    """把 workspace 路径规范化为仅用于作用域比较的稳定字符串。"""

    return os.path.normcase(os.path.abspath(os.path.normpath(path)))
