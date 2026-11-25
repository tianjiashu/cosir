"""单次工具调用的唯一执行入口：准入 → 开始前查取消 → running → 调 handler → 输出预算。

handler 在调用线程里直接执行：没有子进程隔离，也没有硬超时强杀。需要时限的工具在 handler 里自己守
（如 ``search_content`` 的截止时间、浏览器的宿主方法时限），需要中途停下的工具自己轮询取消信号
（终端命令经 ``should_stop_call``）。门禁规则归 ``ToolAccessGate``、预算算法归 ``ToolOutputBudget``，
本模块只编排它们并承担 handler 调用本身。

running 由这里在真正调用 handler 之前经 workflow ``emit`` 发出，终态由调用方（``tools_node``）发出；
两者都只是 core 的执行事件，tools 因此不依赖 Transport，也因为要 ``emit``，执行器只能在 workflow
作用域内调用。门禁拒绝、取消与执行结果都经过模型输出预算。
"""

import json
import traceback
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from app.config.logging.logger import log
from app.core.events import ToolCallStatusChangedEvent
from app.core.tool.execute.tool_access_gate import ToolAccessGate
from app.core.tool.execute.tool_output_budget import ToolOutputBudget
from app.core.tool.external_tools import external_tools
from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import ToolCall, ToolExecutionContext, ToolObservation
from app.core.tool.schemas.request_tools import RequestTools
from app.core.tool.tool_registry import ToolRegistry
from app.core.workflow.cancel.run_cancellation_registry import cancellation_registry
from app.core.workflow.cancel.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.core.workflow.node_helper import emit
from app.models.enums.error_kind import ErrorKind
from app.models.message_context.message_metadata.tool_message_metadata import ToolMessageMetadata

# 取消只在调用 handler 之前检查，所以检出时调用一定还没执行：如实说没跑过，模型恢复后才敢在需要时重做。
_NOT_STARTED_REASON = (
    "the user stopped the session before this tool call started, so it was not executed and "
    "has no effects. Call it again later only if it is still needed."
)


class ToolExecutor:
    """模型请求工具调用的统一执行入口。不负责终态事件（调用方经 ``emit``）与 handler 业务逻辑。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self._gate = ToolAccessGate(registry)
        self._budget = ToolOutputBudget()

    def execute(
        self,
        call: ToolCall,
        execution_context: ToolExecutionContext,
        request_tools: RequestTools,
    ) -> ToolObservation:
        """执行单次工具调用并返回归一化观察，``content`` 一律经过输出预算，``display_data`` 不截断。

        ``execution_context`` 由调用方按 ``call`` 构造（含 ``tool_call_id``）。
        门禁拒绝与开始前的取消都不发 running：只有真正调用 handler 的调用才算开始运行，卡片不会先闪一下
        「运行中」。handler 抛的异常收口为 error 观察；执行链自身的异常原样上抛，由调用方收口。

        内置工具调 handler（参数展开成关键字参数）；已加载的外部工具交给端口（``external_tools().execute``，
        参数整包传入）。两者之后的取消检查、running、异常收口与预算完全相同。
        """

        gate_outcome = self._gate.evaluate(call, execution_context, request_tools)
        if gate_outcome.denial is not None:
            return self._budget.apply(gate_outcome.denial, execution_context)
        arguments = gate_outcome.arguments
        invoke: Callable[[], Any]
        if gate_outcome.external is not None:
            external = gate_outcome.external
            tool_name = external.name
            invoke = lambda: external_tools().execute(external, arguments, execution_context)  # noqa: E731
        else:
            tool = gate_outcome.tool
            assert tool is not None  # 通过准入时两者恰有一个
            tool_name = tool.name
            invoke = lambda: tool.handler(**arguments, execution_context=execution_context)  # noqa: E731
        if cancellation_registry.is_cancelled(execution_context.session_id):
            log.info(
                "tool_execution_cancelled",
                extra={
                    "msg": "Session 已停止，工具调用在开始前中止",
                    "data": {
                        "tool_name": tool_name,
                        "tool_call_id": execution_context.tool_call_id,
                    },
                },
            )
            observation = tool_cancelled(
                tool_name, tool_call_id=execution_context.tool_call_id, reason=_NOT_STARTED_REASON
            )
            return self._budget.apply(observation, execution_context)

        emit(
            ToolCallStatusChangedEvent(
                session_id=execution_context.session_id,
                run_id=execution_context.run_id,
                step_id=execution_context.step_id,
                tool_call_id=execution_context.tool_call_id,
                result=ToolMessageMetadata(status="running"),
            )
        )
        observation = self._run_handler(tool_name, invoke, execution_context)
        return self._budget.apply(observation, execution_context)

    def _run_handler(
        self, tool_name: str, invoke: Callable[[], Any], execution_context: ToolExecutionContext
    ) -> ToolObservation:
        """调用 handler（或延迟工具的来源）；期间登记为本 Session 执行中的调用，退出前释放登记与工具级取消信号。

        handler 跑完就保留真实结果，即使执行期间用户停止了 Session：副作用已经发生，改判为取消会让模型
        误以为操作没执行；Run 级停止交给 workflow 的下一个检查点收敛。
        """

        tool_call_id = execution_context.tool_call_id
        tool_call_cancellation_registry.begin(execution_context.session_id, tool_call_id)
        try:
            result = invoke()
        except Exception as exc:
            log.error(
                "tool_handler_failed",
                extra={
                    "msg": "工具 handler 执行抛异常",
                    "data": {
                        "error_kind": ErrorKind.RUNTIME_FAILED.value,
                        "tool_name": tool_name,
                        "traceback": traceback.format_exc(),
                    },
                },
            )
            return tool_error(
                tool_name,
                f"the tool handler raised an exception: {exc}. Inspect the underlying tool failure "
                "and change the plan before calling the tool again.",
                tool_call_id=tool_call_id,
            )
        finally:
            tool_call_cancellation_registry.clear(execution_context.session_id, tool_call_id)
        return self._normalize_result(tool_name, result, tool_call_id)

    @staticmethod
    def _normalize_result(tool_name: str, payload: Any, tool_call_id: str) -> ToolObservation:
        """观察透传（缺 ``tool_call_id`` 时注入），dict/list 经 ``json.dumps``，其余经 ``str()`` 包成成功观察。"""

        if isinstance(payload, ToolObservation):
            if tool_call_id and not payload.tool_call_id:
                return replace(payload, tool_call_id=tool_call_id)
            return payload
        if isinstance(payload, dict | list):
            content = json.dumps(payload, ensure_ascii=False, default=str)
        else:
            content = str(payload)
        return tool_success(tool_name, content, tool_call_id=tool_call_id)
