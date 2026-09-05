"""工具调用 trace 记录器协议（service 层自定、自消费，零三方依赖）。

依赖倒置：可观测性实现收口在 ``core/observability``，service 层只定义窄协议并消费，不直接
import Langfuse。``core/observability.LangfuseToolTraceRecorder`` 按结构化子类型满足本协议
（``core → service`` 为允许方向），无任何反向依赖。

空实现（``_NullToolSpan`` / ``_NullToolTraceRecorder``）与协议同文件定义、由 service 自消费，
``WorkflowOperations`` 与 ``LangfuseToolTraceRecorder`` 的降级分支共用同一份，避免空壳类在
多个包重复漂移（不重复造轮子）。
"""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Protocol, runtime_checkable

from app.core.tools.schemas import ToolCall, ToolObservation


@runtime_checkable
class ToolCallSpan(Protocol):
    """一次工具调用 span 的开放接口（由 recorder 实现）。"""

    def record(self, observation: ToolObservation) -> None:
        """把工具执行的观察结果写入当前 span。

        参数:
            observation: 工具系统产出的归一化观察结果。

        返回:
            无。

        异常:
            无。

        副作用:
            将观察结果映射进 span 的 output / level（具体由实现决定）。
        """
        ...


@runtime_checkable
class ToolTraceRecorder(Protocol):
    """工具调用 trace 记录器协议（依赖倒置，避免 service 耦合 Langfuse）。"""

    def span(self, call: ToolCall, step_id: str) -> AbstractContextManager[ToolCallSpan]:
        """为一次工具调用打开追踪 span；上下文退出即结束计时。

        参数:
            call: 模型请求的工具调用。
            step_id: 步骤标识。

        返回:
            包裹 ``ToolCallSpan`` 的上下文管理器。

        异常:
            无。

        副作用:
            由实现决定（创建 span / 计时等）。
        """
        ...

    def flush(self) -> None:
        """Flush recorder-side buffered trace data if the implementation has any.

        参数:
            无。

        返回:
            无。

        异常:
            实现不应向上传播异常；调用方仍会做最终兜底，避免观测失败影响主流程。

        副作用:
            可能触发外部观测系统的缓冲上报。
        """
        ...


class _NullToolSpan(ToolCallSpan):
    """空工具 span（降级路径），``record`` 为 no-op。

    与协议同文件定义、由 service 自消费：未注入真实 recorder 时，``WorkflowOperations``
    与 ``LangfuseToolTraceRecorder`` 的降级分支共用同一份空实现，避免空壳类在多个包重复漂移。
    """

    def record(self, observation: ToolObservation) -> None:
        """空实现：忽略工具观察结果，不产生 trace。

        参数:
            observation: 工具观察结果（被忽略）。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """
        return None


class _NullToolTraceRecorder:
    """空实现 ``ToolTraceRecorder`` 协议：``span`` 退化为 no-op 上下文。

    当未注入真实 recorder 时由 ``WorkflowOperations`` 默认使用，确保循环体只有 ``with``
    一条路径、零 trace 开销，且行为与集成前完全一致。
    """

    @contextmanager
    def span(self, call: ToolCall, step_id: str) -> Iterator[_NullToolSpan]:
        """返回空 span 上下文。

        参数:
            call: 工具调用（被忽略）。
            step_id: 步骤标识（被忽略）。

        生成:
            一个 ``_NullToolSpan`` 实例。

        异常:
            无。

        副作用:
            无。
        """
        yield _NullToolSpan()

    def flush(self) -> None:
        """空实现：无缓冲数据需要上报。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """
        return None
