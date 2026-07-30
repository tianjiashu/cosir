"""工具调用 trace 的 Langfuse 实现（实现 service 层 ``ToolTraceRecorder`` 协议）。

本模块是 Langfuse 三方依赖出现的唯一另一处（与 ``langfuse_tracing`` 并列），收口在
``core/observability``。职责：把每次工具调用记录为 Langfuse tool observation，含参数、结果、
状态、耗时（observation 自动计时）；不负责工具执行本身、事件发出或 trace 根上下文（由
``turn_trace`` 建立）。

分层约束：``core → service`` 方向合法，本模块实现 service 定义的 ``ToolTraceRecorder`` 协议，
不反向依赖 service 运行时。所有 langfuse import 惰性加载；工具 observation 内部异常降级为
空 observation，绝不中断工具执行。

Langfuse v4 API：工具 observation 用 ``client.start_as_current_observation(as_type="tool", ...)``，
上下文退出时自动结束，无需手动 ``end()``；结果经 ``span.update(output=, level=, status_message=,
metadata=)`` 写入。
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.config.logging.logger import log
from app.config.settings import Settings
from app.service.tool_execution.tool_trace_recorder import _NullToolSpan
from app.tools.schemas import ToolCall, ToolObservation
from app.trace_infra.redaction import redact_terminal_output


class _LangfuseToolSpan:
    """Langfuse tool observation 的适配对象（实现 ``ToolCallSpan`` 协议）。

    仅负责把 ``ToolObservation`` 映射进 observation 的 output / level，并在上下文退出时结束
    observation。不感知 Langfuse 客户端创建细节（由 recorder 持有）。
    """

    def __init__(self, span: Any) -> None:
        self._span = span
        self._observation: ToolObservation | None = None

    def record(self, observation: ToolObservation) -> None:
        """记录工具执行的观察结果（延迟到 observation 结束时统一写入 output）。

        参数:
            observation: 工具系统产出的归一化观察结果。

        返回:
            无。

        异常:
            无。

        副作用:
            暂存 observation，供 ``finalize`` 写入 observation output。
        """
        self._observation = observation

    def finalize(self) -> None:
        """把暂存的观察结果写入 output / level（observation 由上下文管理器自动结束）。

        参数:
            无。

        返回:
            无。

        异常:
            无（``span.update`` 失败由 ``span()`` 上下文的兜底捕获）。

        副作用:
            写 observation output、设置 level/status_message。observation 的结束由
            ``start_as_current_observation`` 上下文管理器在退出时自动完成。
        """
        observation = self._observation
        if observation is None:
            return
        output = {
            "content": redact_terminal_output(observation.content),
            "data": observation.display_data or {},
            "error": observation.error,
            "reason": observation.reason,
            "retryable": observation.retryable,
        }
        self._span.update(
            output=output,
            level="ERROR" if observation.status == "error" else "DEFAULT",
            metadata={"permission": observation.permission},
        )
        if observation.status == "error" and observation.error:
            self._span.update(status_message=observation.error)


class LangfuseToolTraceRecorder:
    """把每次工具调用记录为 Langfuse tool observation（实现 ``ToolTraceRecorder`` 协议）。

    职责边界：
    - 负责：observation 开闭、输入输出映射、错误级别标注、输出脱敏、进程级客户端复用与 flush。
    - 不负责：工具执行本身、事件发出、trace 根上下文（由 ``turn_trace`` 建立）。
    """

    def __init__(self) -> None:
        """创建进程级 Langfuse 客户端（仅当 ``tracing_enabled()`` 为 True 时由调用方构造）。

        参数:
            无。

        返回:
            无。

        异常:
            不向上抛出：客户端创建失败由 ``span`` 的兜底捕获并降级为空 observation。

        副作用:
            创建 Langfuse 客户端实例（构造不连网，懒连接；以 public_key 为进程单例键）。
        """
        from langfuse import Langfuse

        self._client = Langfuse(
            public_key=Settings.LANGFUSE_PUBLIC_KEY,
            secret_key=Settings.LANGFUSE_SECRET_KEY,
            base_url=Settings.LANGFUSE_BASE_URL,
        )

    @contextmanager
    def span(self, call: ToolCall, step_id: str) -> Iterator[Any]:
        """为一次工具调用打开追踪 observation；上下文退出即结束计时。

        参数:
            call: 模型请求的工具调用（含工具名、参数、调用 id）。
            step_id: 步骤标识，用于把 tool observation 关联回对应模型步。

        生成:
            一个 ``ToolCallSpan`` 适配对象；成功时为 ``_LangfuseToolSpan``，
            失败时为 ``_NullToolSpan``。

        异常:
            不向上抛出：Langfuse 客户端/observation 异常被内部捕获并降级为空 observation。

        副作用:
            创建 tool observation（自动挂到当前 OTel 根 observation 下）；退出上下文时
            自动结束并测量耗时。
        """
        try:
            arguments = call.arguments if isinstance(call.arguments, dict) else {}
            with self._client.start_as_current_observation(
                as_type="tool",
                name=call.tool_name,
                input={"arguments": arguments, "call_id": call.call_id},
                metadata={"step_id": step_id, "tool_call_id": call.call_id},
            ) as span:
                adapter = _LangfuseToolSpan(span)
                try:
                    yield adapter
                finally:
                    adapter.finalize()
        except Exception:
            log.exception(
                "langfuse_tool_span_failed",
                extra={
                    "msg": "Langfuse 工具 observation 创建或结束失败，降级为空 observation",
                    "data": {"tool_name": call.tool_name, "step_id": step_id},
                },
            )
            yield _NullToolSpan()

    def flush(self) -> None:
        """flush 本 recorder 持有的 Langfuse 客户端缓冲。

        参数:
            无。

        返回:
            无。

        异常:
            不向上抛出：flush 失败仅记日志。

        副作用:
            触发后台批量上报。
        """
        try:
            self._client.flush()
        except Exception:
            log.exception(
                "langfuse_recorder_flush_failed",
                extra={"msg": "Langfuse recorder flush 失败（忽略）"},
            )
