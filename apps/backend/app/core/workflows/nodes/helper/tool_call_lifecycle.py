"""工具调用生命周期状态与事件发射。

``ToolCallLifecycleManager`` 是 LangGraph state 中的可序列化生命周期快照，也是工具
生命周期事件的唯一发射入口。它只保存工具调用的身份、参数和状态；
``operations``、``stream_writer``、``runtime_context`` 等运行期对象不进入 checkpoint，
事件方法执行时从当前 graph config 获取这些依赖。

创建事件本身把 Transport part 初始化为 ``pending``，所以不再单独发射 pending 状态事件。
状态迁移遵循 ``pending -> running -> completed/failed/cancelled``，每个方法都返回一个新的
manager，节点必须把返回值写回 graph state。
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Mapping
from typing import Any, Literal

from langgraph.config import get_stream_writer
from pydantic import BaseModel, ConfigDict, Field

from app.assistant_transport.event import ToolCallCreatedEvent, ToolCallStatusChangedEvent
from app.config.logging.logger import log
from app.core.context.runtime_context_manager import RuntimeContextManager
from app.core.tools.schemas import ToolCall, ToolObservation
from app.core.workflows.nodes.helper.common import _runtime_config, _runtime_context
from app.core.workflows.workflow_operations import WorkflowOperations
from app.models.enums.tool_call_status import ToolCallEventStatus


class ToolCallLifecycleRecord(BaseModel):
    """单次工具调用的可序列化生命周期记录。"""

    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    status: ToolCallEventStatus = "pending"
    args: dict[str, object] = Field(default_factory=dict)
    presentation: dict[str, object] = Field(default_factory=dict)


@dataclasses.dataclass
class SettlementResult:
    """一批观察结算后的计数与生命周期快照。

    ``lifecycle`` 不参与相等比较，兼容调用方只比较计数的既有契约；observe 节点使用它
    把结算后的 manager 写回 LangGraph state。
    """

    tool_error_count: int
    error_count: int
    lifecycle: Any = dataclasses.field(default=None, compare=False, repr=False)


def _event_status(status: str) -> Literal["completed", "failed", "cancelled"]:
    """把观察摘要状态映射为事件契约的终态状态。"""

    if status == "success":
        return "completed"
    if status == "cancelled":
        return "cancelled"
    return "failed"


def _ui_data(summary: dict[str, Any]) -> dict[str, object] | None:
    """返回终态事件需要更新的 ``display_data``；没有数据时返回 ``None``。"""

    display_data = summary.get("display_data")
    if not isinstance(display_data, Mapping):
        return None
    return copy.deepcopy(dict(display_data))


def _ui_error(summary: dict[str, Any], event_status: str) -> str | None:
    """只生成事件层的短错误提示；完整诊断保留在模型消息。"""

    if event_status == "cancelled":
        return "已取消"
    if event_status != "failed":
        return None
    display_data = summary.get("display_data")
    if isinstance(display_data, Mapping) and isinstance(display_data.get("status_hint"), str):
        return display_data["status_hint"]
    return "执行失败"


def _summary_to_observation(summary: dict[str, Any]) -> ToolObservation:
    """把观察摘要转回 ``ToolObservation``，供模型上下文写入 ToolMessage。

    摘要键名与执行层观察字段一致（``tool_call_id`` 等），由 ``tools`` 节点对
    ``ToolObservation`` 的 ``dataclasses.asdict`` 投影产出；缺少必需键时按契约错误抛
    ``KeyError``，由调用方（workflow 终态路径）记录并落定失败。
    """

    return ToolObservation(
        tool_name=summary["tool_name"],
        status=summary["status"],
        content=summary["content"],
        error=summary["error"],
        reason=summary["reason"],
        retryable=summary["retryable"],
        tool_call_id=summary["tool_call_id"],
        display_data=copy.deepcopy(_ui_data(summary)),
    )


class ToolCallLifecycleManager(BaseModel):
    """工具调用生命周期的 LangGraph state 与事件发射门面。

    state 中只保留 ``calls``，键为 ``tool_call_id``，值为可序列化记录。所有状态方法都
    返回深拷贝后的新 manager，避免节点继续持有旧快照；调用节点必须将返回值放入返回的
    state patch。事件发射和模型上下文写回是方法的运行期副作用，依赖从当前 LangGraph
    execution context 解析，不会被 Pydantic 或 LangGraph 序列化。

    异常:
        pydantic.ValidationError: manager state 或事件字段不满足契约时抛出。
        KeyError: 观察摘要缺少模型上下文所需字段时由上游契约错误触发。
    """

    model_config = ConfigDict(extra="forbid")

    calls: dict[str, ToolCallLifecycleRecord] = Field(default_factory=dict)

    def _copy(self) -> ToolCallLifecycleManager:
        """复制 state，确保生命周期迁移以新快照返回。"""

        return self.model_copy(deep=True)

    @staticmethod
    def _valid_tool_name(tool_name: object) -> bool:
        """判断工具名是否属于当前运行时注册工具。"""

        if not isinstance(tool_name, str) or not tool_name:
            return False
        operations: WorkflowOperations = _runtime_config().operations
        return tool_name in {tool.name for tool in operations.model_tools or []}

    @staticmethod
    def _presentation_for(tool_name: str) -> dict[str, object]:
        """读取工具的静态展示声明。"""

        operations: WorkflowOperations = _runtime_config().operations
        for definition in getattr(operations, "model_tools", ()) or ():
            if definition.name == tool_name and definition.display is not None:
                return definition.display.to_dict()
        return {}

    def create(
        self,
        *,
        task_id: int,
        run_id: int,
        step_id: str,
        raw_tool_calls: list[dict[str, Any]],
    ) -> ToolCallLifecycleManager:
        """为已确认身份的模型工具调用发出创建事件并初始化为 pending。

        ``raw_tool_calls`` 可来自单个流式 chunk；一个 chunk 中的多个调用会逐条处理。只有
        同时具备非空 ``id``、``name`` 且 name 已注册的调用才会建立记录。重复 id 幂等跳过。
        """

        updated = self._copy()
        stream_writer = get_stream_writer()
        for raw_call in raw_tool_calls:
            call_id = raw_call.get("id")
            tool_name = raw_call.get("name")
            if (
                not isinstance(call_id, str)
                or not call_id
                or not self._valid_tool_name(tool_name)
                or call_id in updated.calls
            ):
                continue
            assert isinstance(tool_name, str)
            presentation = self._presentation_for(tool_name)
            stream_writer(
                ToolCallCreatedEvent(
                    task_id=task_id,
                    run_id=run_id,
                    step_id=step_id,
                    tool_call_id=call_id,
                    tool_name=tool_name,
                    presentation=copy.deepcopy(presentation),
                )
            )
            updated.calls[call_id] = ToolCallLifecycleRecord(
                tool_call_id=call_id,
                tool_name=tool_name,
                presentation=presentation,
            )
        return updated

    def _emit_status(
        self,
        *,
        task_id: int,
        run_id: int,
        step_id: str,
        call_id: str,
        to_status: ToolCallEventStatus,
        args: dict[str, object] | None = None,
        error: str | None = None,
        display_data: dict[str, object] | None = None,
    ) -> None:
        """发射一条状态迁移事件；合法迁移由 snapshot projector 校验。"""

        get_stream_writer()(
            ToolCallStatusChangedEvent(
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                tool_call_id=call_id,
                status=to_status,
                args=copy.deepcopy(args) if args is not None else None,
                error=error,
                display_data=display_data,
            )
        )

    def begin(
        self,
        *,
        task_id: int,
        run_id: int,
        step_id: str,
        tool_calls: list[ToolCall],
    ) -> ToolCallLifecycleManager:
        """把 pending 调用迁移到 running，并写入完整解析后的参数。"""

        updated = self
        for tool_call in tool_calls:
            if tool_call.call_id not in updated.calls:
                updated = updated.create(
                    task_id=task_id,
                    run_id=run_id,
                    step_id=step_id,
                    raw_tool_calls=[{"id": tool_call.call_id, "name": tool_call.tool_name}],
                )
            record = updated.calls.get(tool_call.call_id)
            if record is None or record.status != "pending":
                continue
            updated._emit_status(
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                call_id=tool_call.call_id,
                to_status="running",
                args=tool_call.arguments,
            )
            updated = updated._copy()
            updated.calls[tool_call.call_id].status = "running"
            updated.calls[tool_call.call_id].args = copy.deepcopy(tool_call.arguments)
        return updated

    def cancel(
        self,
        *,
        task_id: int,
        run_id: int,
        step_id: str,
        tool_calls: list[ToolCall],
    ) -> ToolCallLifecycleManager:
        """把尚未结束的调用迁移到 cancelled。"""

        updated = self._copy()
        for tool_call in tool_calls:
            record = updated.calls.get(tool_call.call_id)
            if record is None or record.status not in {"pending", "running"}:
                continue
            updated._emit_status(
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                call_id=tool_call.call_id,
                to_status="cancelled",
            )
            updated.calls[tool_call.call_id].status = "cancelled"
        return updated

    def settle(
        self,
        *,
        task_id: int,
        run_id: int,
        step_id: str,
        summary: dict[str, Any],
    ) -> tuple[ToolCallLifecycleManager, Literal["completed", "failed", "cancelled"]]:
        """发出终态事件、更新 state，并把 ToolMessage 写回模型上下文。"""

        observation = _summary_to_observation(summary)
        event_status = _event_status(observation.status)
        call_id = summary["tool_call_id"]
        existing = self.calls.get(call_id)
        if existing is not None and existing.status in {"completed", "failed", "cancelled"}:
            return self._copy(), event_status
        updated = self._copy()
        record = updated.calls.get(call_id)
        if record is None:
            # 正常路径一定先 create；保留记录可让恢复后的 state 反映实际终态。
            presentation = self._presentation_for(summary["tool_name"])
            updated.calls[call_id] = ToolCallLifecycleRecord(
                tool_call_id=call_id,
                tool_name=summary["tool_name"],
                status="pending",
                presentation=presentation,
            )
        runtime_context: RuntimeContextManager = _runtime_context()
        operations: WorkflowOperations = _runtime_config().operations
        record = updated.calls[call_id]
        record.status = event_status
        result_display_data = _ui_data(summary)
        status_hint = _ui_error(summary, event_status)
        if event_status != "completed":
            result_display_data = {"status_hint": status_hint} if status_hint else None
        tool_result = {
            "status": observation.status,
            "display_data": result_display_data,
            "status_hint": status_hint,
            # Full observation errors are model-facing diagnostics and may contain provider
            # details. Transport metadata is durable UI data, so only the controlled hint is
            # persisted here; the ToolMessage retains the diagnostic for the model.
            "error": None,
        }
        created = runtime_context.add_message(
            operations.to_tool_model_message(observation),
            tool_result=tool_result,
        )
        if created is False:
            return updated, event_status
        log.info(
            "tool_observation_persisted",
            extra={
                "msg": "canonical tool observation 已持久化",
                "data": {
                    "task_id": task_id,
                    "run_id": run_id,
                    "tool_call_id": call_id,
                    "status": event_status,
                },
            },
        )
        # DB-backed context write is deliberately before the event so the projector never
        # publishes a terminal tool state that cannot be rebuilt from context.
        try:
            updated._emit_status(
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                call_id=call_id,
                to_status=event_status,
                error=status_hint,
                display_data=copy.deepcopy(result_display_data),
            )
        except Exception:
            log.exception(
                "tool_terminal_event_failed",
                extra={
                    "msg": "工具结果已落库，终态 Transport 事件发送失败并被降级",
                    "data": {
                        "task_id": task_id,
                        "run_id": run_id,
                        "tool_call_id": call_id,
                        "status": event_status,
                    },
                },
            )
        return updated, event_status

    def settle_batch(
        self,
        *,
        task_id: int,
        run_id: int,
        step_id: str,
        summaries: list[dict[str, Any]],
        inherited_error_count: int,
    ) -> SettlementResult:
        """结算一批观察摘要，返回错误计数和更新后的 lifecycle。"""

        lifecycle = self
        tool_error_count = inherited_error_count
        error_count = 0
        for summary in summaries:
            lifecycle, event_status = lifecycle.settle(
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                summary=summary,
            )
            if event_status == "completed":
                tool_error_count = 0
            elif event_status == "failed":
                tool_error_count += 1
                error_count += 1

        log.info(
            "observe_node_dispatch_completed",
            extra={
                "msg": f"工具观察分发完成，step_id={step_id}",
                "data": {
                    "step_id": step_id,
                    "result_count": len(summaries),
                    "error_count": error_count,
                    "tool_error_count": tool_error_count,
                },
            },
        )
        return SettlementResult(
            tool_error_count=tool_error_count,
            error_count=error_count,
            lifecycle=lifecycle,
        )
