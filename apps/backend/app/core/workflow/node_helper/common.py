"""一次驱动的进程内依赖作用域；数据事实保存在 Run 和 context，作用域不参与恢复。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from app.config.logging.logger import log
from app.core.events import WorkflowEventEnvelope
from app.models.enums.react_route import ReactRoute

if TYPE_CHECKING:
    from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager
    from app.core.workflow.execution_state.runtime_context import RuntimeContext

_runtime: ContextVar[RuntimeContext] = ContextVar("workflow_runtime")
_context: ContextVar[RuntimeMessageContextManager] = ContextVar("workflow_context")


@contextmanager
def execution_scope(runtime: RuntimeContext, context: RuntimeMessageContextManager) -> Iterator[None]:
    """同一驱动及其工具线程共享依赖；退出必定恢复上层作用域。"""
    rt = _runtime.set(runtime)
    ct = _context.set(context)
    try:
        yield
    finally:
        _context.reset(ct)
        _runtime.reset(rt)


def _runtime_context() -> RuntimeContext:
    return _runtime.get()


def _message_context() -> RuntimeMessageContextManager:
    return _context.get()


def emit(event: WorkflowEventEnvelope) -> None:
    """把执行事件交给装配方注入的 sink；同步投影，线程安全由 Task snapshot 锁保证。

    事件是旁路：canonical facts 已由各自的持久化 owner 写入，sink 失败只记录降级，不能把异常
    传播回工作流节点（AGENTS.md：观测失败不得阻断 Agent 主流程）。
    """
    # 取 sink 不在 try 内：脱离执行作用域调用是装配错误，应直接暴露而不是被当成 sink 失败吞掉。
    sink = _runtime.get().event_sink
    try:
        sink(event)
    except Exception:
        log.exception(
            "workflow_event_sink_failed",
            extra={
                "msg": "执行事件 sink 处理失败，已降级继续执行",
                "data": {
                    "event_type": type(event).__name__,
                    "task_id": event.task_id,
                    "run_id": event.run_id,
                },
            },
        )


def route_state(step_count: int, next_node: ReactRoute) -> dict[str, Any]:
    return {"step_count": step_count, "next_node": next_node}


class RunStopped(Exception):
    """协作取消的退出信号；节点负责取消收口，workflow 结束驱动，不作为执行失败。"""
