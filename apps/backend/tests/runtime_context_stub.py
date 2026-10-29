"""节点 / 工作流测试用的 ``RuntimeContext`` 替身：字段名与生产类型一致，协作者是最小桩。

用例只覆盖它关心的字段（``runtime_stub(allows_tools=..., run_state=...)``）；其余字段给出不会
触发真实依赖的默认值。取消判定读 ``cancelled`` 标志，用例直接改它即可。

生产节点直接取用进程级单例（Run 状态写入、外部消息队列、工具注册表与执行器），不经
``RuntimeContext``。本替身仍接受 ``run_state`` / ``message_queue`` / ``tool_executor`` /
``tool_definitions`` 这些覆盖项，并把它们装为对应单例（经 :data:`SINGLETONS` 打补丁，``conftest``
在每个用例结束后复原），保证节点测试不触碰真实数据库与工具装配。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.config import configuration
from app.core.workflow.execution_state.conversation_run_usage_stats import (
    ConversationRunUsageStats,
)
from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.service import depends as service_depends


def run_state_stub(**overrides: Any) -> SimpleNamespace:
    """Run 状态服务替身：迁移默认返回一个非 None 记录（视为命中）。"""
    stub = SimpleNamespace(
        transition=lambda *_args, **_kwargs: object(),
        save_execution=lambda *_args, **_kwargs: None,
        update_completed_result=lambda *_args, **_kwargs: None,
    )
    stub.__dict__.update(overrides)
    return stub


# 用例级单例补丁；``conftest.reset_runtime_singletons`` 在每个用例结束后 ``undo``。
SINGLETONS = pytest.MonkeyPatch()


class _StubRegistry:
    """按名索引的工具定义替身，只覆盖节点用到的注册表读取接口。"""

    def __init__(self, definitions: dict[str, Any]) -> None:
        self._definitions = definitions

    def get_tool_definition(self, name: str) -> Any:
        return self._definitions.get(name)

    def get_all_tool_names(self) -> list[str]:
        return sorted(self._definitions)


class _StubRegistryProxy:
    """注册表替身：每次读取都取 ``rc`` 的当前工具定义，用例创建替身后改字段仍生效。"""

    def __init__(self, rc: SimpleNamespace) -> None:
        self._rc = rc

    def __getattr__(self, name: str) -> Any:
        return getattr(_StubRegistry(self._rc.tool_definitions), name)


class _StubExecutorProxy:
    """执行器替身：转发到 ``rc`` 当前的 ``tool_executor``。"""

    def __init__(self, rc: SimpleNamespace) -> None:
        self._rc = rc

    def __getattr__(self, name: str) -> Any:
        return getattr(self._rc.tool_executor, name)


def singleton_getter(factory: Any) -> Any:
    """把替身工厂包成可替换 ``depends`` 单例 getter 的函数：保留 ``cache_clear`` 接口。

    ``depends`` 的 getter 都是 ``lru_cache`` 函数，进程级复位夹具会对它们调 ``cache_clear``；
    替身每次调用都现取，不缓存。
    """

    def getter() -> Any:
        return factory()

    getter.cache_clear = lambda: None  # type: ignore[attr-defined]
    return getter


def install_runtime_singletons(rc: SimpleNamespace) -> None:
    """把替身上的协作者装为节点直接取用的进程级单例（读取 ``rc`` 当前属性）。"""

    SINGLETONS.setattr(
        service_depends,
        "get_conversation_run_ledger",
        singleton_getter(lambda: rc.run_state),
    )
    SINGLETONS.setattr(
        service_depends, "get_task_message_queue", singleton_getter(lambda: rc.message_queue)
    )
    SINGLETONS.setattr(configuration, "_TOOL_REGISTRY", _StubRegistryProxy(rc))
    SINGLETONS.setattr(configuration, "_TOOL_EXECUTOR", _StubExecutorProxy(rc))


def runtime_stub(**overrides: Any) -> SimpleNamespace:
    rc = SimpleNamespace(
        run=SimpleNamespace(
            id=2, task_id=1, execution_data=ReactExecutionState(), usage=None
        ),
        # 默认不是侧栏对话 Task：不触发标题支线，需要它的用例显式覆盖 task。
        task=SimpleNamespace(id=1, task_type="delegate_task", title="测试对话"),
        workspace=SimpleNamespace(id=1, root_path="."),
        agent=SimpleNamespace(
            structured_output=None,
            max_steps=10,
            model=SimpleNamespace(model_config_id=1),
        ),
        allows_tools=frozenset(),
        tool_definitions={},
        tool_executor=None,
        run_state=run_state_stub(),
        message_queue=SimpleNamespace(ack=lambda _session, ids: set(ids)),
        mailbox=RunMailbox(),
        task_space=SimpleNamespace(task_tool_schemas=()),
        event_sink=lambda _event: None,
        model=None,
        thinking_channel="",
        usage_stats=ConversationRunUsageStats(),
        title_branch=None,
        resume_decision=None,
        cancelled=False,
    )
    rc.__dict__.update(overrides)
    rc.is_cancelled = lambda: rc.cancelled
    install_runtime_singletons(rc)
    return rc
