"""一次驱动的进程内依赖作用域；数据事实保存在 Run 和 context，作用域不参与恢复。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from app.core.workflow.execution_state.react_route import ReactRoute

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


def get_stream_writer() -> Callable[[Any], Any]:
    """事件同步交给既有 projector；线程安全由 Task snapshot 锁保证。"""
    return _runtime.get().operations.process_event


def route_state(step_count: int, next_node: ReactRoute) -> dict[str, Any]:
    return {"step_count": step_count, "next_node": next_node}


class RunStopped(Exception):
    """协作取消的退出信号；节点负责取消收口，workflow 结束驱动，不作为执行失败。"""
