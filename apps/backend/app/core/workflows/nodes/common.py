"""ReAct-like 工作流节点间共享的运行时辅助。

本模块只承载「两个节点（model / tools）都要用」的公共原语，不包含任何单节点专属逻辑：

- ``write_event`` / ``_make_write_event``：统一的事件写入结构。
- ``_runtime_config`` / ``_runtime_context``：从 LangGraph 运行上下文取运行时配置与
  task 级上下文。

节点各自的数据处理辅助（如 ``_extract_text``、``_finalize_ai_message``）不放这里，
归属见 ``model_node`` / ``tools_node``。
"""

from collections.abc import Callable
from typing import TYPE_CHECKING

from langgraph.config import get_config, get_stream_writer

from app.models.enums.event_type import EventType
from app.models.payload.runtime_event_payload import RuntimeEventPayload

from ..react.runtime_config import RuntimeConfig

if TYPE_CHECKING:
    from app.core.context.runtime_context import RuntimeContext


# 内部封装：统一事件写入结构。
def write_event(event_type: EventType, payload: RuntimeEventPayload) -> None:
    """在当前 LangGraph 运行上下文写出一条自定义业务事件。

    参数:
        event_type: 事件类型枚举。
        payload: 事件负载值对象。

    返回:
        无。

    异常:
        RuntimeError: 在无 LangGraph 运行上下文处调用时由 ``get_stream_writer()`` 抛出。

    副作用:
        经 ``get_stream_writer()`` 写入 ``custom`` 事件流。
    """
    writer = get_stream_writer()  # 自定义事件写入器
    writer({"event_type": str(event_type), "payload": payload})


def _make_write_event() -> Callable[[EventType, RuntimeEventPayload], None]:
    """在当前 LangGraph 运行上下文中取出 writer，返回可跨线程调用的事件写入回调。

    ``write_event`` 每次调用都现取 ``get_stream_writer()``，只能在持有 LangGraph
    运行上下文的协程内使用。当节点把工作交给 ``asyncio.to_thread`` 时，需要先在
    协程内取出 writer 对象并由闭包持有，工作线程才能继续写 ``custom`` 事件流。

    返回:
        与 ``write_event`` 同签名的回调，内部使用已捕获的 writer。

    异常:
        RuntimeError: 在无 LangGraph 运行上下文处调用时由 ``get_stream_writer()`` 抛出。

    副作用:
        无（调用返回的回调时才写入事件流）。
    """
    writer = get_stream_writer()

    def _write(event_type: EventType, payload: RuntimeEventPayload) -> None:
        writer({"event_type": str(event_type), "payload": payload})

    return _write


def _runtime_config() -> RuntimeConfig:
    """从 LangGraph 运行上下文取出 ReAct 工作流注入的运行时配置容器。

    ``ReactLikeWorkflow.run()`` 把 ``RuntimeConfig`` 放入 config 的 ``runtime_config``；
    节点统一经本函数取出，避免在各节点里用裸字符串 key 重复读取 ``config["configurable"]``。

    返回:
        当前 graph 执行注入的 ``RuntimeConfig`` 实例。
    """
    # 从 LangGraph 注入的 config 中取出预先放好的 RuntimeConfig。
    return get_config()["configurable"]["runtime_config"]


def _runtime_context() -> "RuntimeContext":
    """从 LangGraph 运行上下文取出 task 级运行时上下文。

    ``ReactLikeWorkflow.run()`` 把 ``RuntimeContext`` 放入 config 的 ``runtime_context``；
    节点统一经本函数取出，与 ``_runtime_config`` 同口径，避免裸字符串 key 重复读取
    ``config["configurable"]``，且使上下文对象不进入 graph state（不兼容消息 reducer）。

    返回:
        当前 graph 执行注入的 ``RuntimeContext`` 实例。
    """
    # 从 LangGraph 注入的 config 中取出预先放好的 RuntimeContext。
    return get_config()["configurable"]["runtime_context"]
