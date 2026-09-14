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
from app.models.conversation_task_context import TransportMetadata
from app.models.enums.tool_call_status import ToolCallEventStatus
from app.utils.trace_infra.redaction import redact_terminal_output

# 非法工具调用参数预览截断长度
INVALID_TOOL_ARGS_PREVIEW_CHARS = 500

# 修复提示整体字符预算上限（超出整体截断并加末尾说明）
INVALID_TOOL_CALL_TOTAL_BUDGET_CHARS = 2000


class ToolCallLifecycleRecord(BaseModel):
    """单次工具调用的可序列化生命周期记录。"""

    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    status: ToolCallEventStatus = "pending"
    args: dict[str, object] = Field(default_factory=dict)
    presentation: dict[str, object] = Field(default_factory=dict)
    # 模型输出中参数非法但工具名合法的调用，挂载原始 invalid_tool_call（name/args/error），
    # 供 observe 节点构造修复提示并收口前端 pending part；合法调用此字段恒为 None。
    invalid_detail: dict[str, object] | None = None


@dataclasses.dataclass
class SettlementResult:
    """一批观察结算后的计数与生命周期快照。

    ``lifecycle`` 不参与相等比较，兼容调用方只比较计数的既有契约；observe 节点使用它
    把结算后的 manager 写回 LangGraph state。
    """

    tool_error_count: int
    error_count: int
    # settle_batch / settle 始终返回非空的 manager（未命中调用会即时补建），无需 Optional。
    lifecycle: ToolCallLifecycleManager = dataclasses.field(compare=False, repr=False)


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


def build_invalid_tool_call_repair_message(
    repair_datas: list[dict[str, Any]],
) -> str:
    """构造要求模型修复非法工具调用的结构化英文提示文本。

    面向模型、纯英文。返回单条 ``str``（不是消息对象），由调用方自行包装为
    ``SystemMessage`` 写进 ``RuntimeContextManager``。结构：

    - 顶部一句总领：说明上次非法工具调用未执行、请重试、只发严格合法 tool_calls。
    - 每个 repair 条目（受 ``INVALID_TOOL_CALL_SUMMARY_LIMIT`` 限条）输出：
      ``## <tool_name>`` + ``name`` / ``args`` 预览（经 ``redact_terminal_output``
      脱敏后截断到 ``INVALID_TOOL_ARGS_PREVIEW_CHARS``、超出加 ``...[truncated]``）/
      ``error``（若有）。``args`` 预览在脱敏后再截断，确保 secret 不进上下文。
    - 整体字符预算受 ``INVALID_TOOL_CALL_TOTAL_BUDGET_CHARS`` 约束：逐条拼接，一旦
      累计超预算即停止追加并附末尾截断说明，保证不超过预算且每条仍含可定位的
      ``tool_name`` 与 ``error`` 关键字段。

    参数:
        repair_datas: 待修复的非法调用明细列表，每项形如
            ``{"tool_name": <命中工具名>,
            "invalid_tool_call": <LangChain invalid_tool_call>}``。

    返回:
        结构化英文提示 ``str``，可直接包装为 ``SystemMessage`` 注入模型上下文。

    异常:
        无（对所有字段做 ``get`` / ``str`` 容错，解析失败的非 JSON 片段也能安全处理）。

    副作用:
        无（只读入参；脱敏与截断均为纯函数式处理，不改外部状态）。
    """
    header = (
        "The previous assistant message contained invalid tool call output that "
        "could not be parsed; the tool calls were NOT executed. Retry this step. "
        "If you still need the tool(s), emit valid tool_calls only with strict JSON "
        "arguments matching the schema. Do not claim a tool or child agent started "
        "unless the call is valid and executed."
    )

    sections: list[str] = []
    total_chars = len(header)
    budget = INVALID_TOOL_CALL_TOTAL_BUDGET_CHARS
    truncated = False

    for repair_data in repair_datas:
        tool_name = repair_data.get("tool_name", "")
        invalid_tc = repair_data.get("invalid_tool_call", {})
        if not isinstance(invalid_tc, dict):
            invalid_tc = {}

        # args 预览：先脱敏再截断，防止 secret 进上下文。
        raw_args = str(invalid_tc.get("args", ""))
        redacted_args = redact_terminal_output(raw_args)
        if len(redacted_args) > INVALID_TOOL_ARGS_PREVIEW_CHARS:
            redacted_args = redacted_args[:INVALID_TOOL_ARGS_PREVIEW_CHARS] + "...[truncated]"

        error = invalid_tc.get("error")
        error_line = f"error: {error}\n" if error else ""

        section = (
            f"## {tool_name}\n" f"name: {tool_name}\n" f"args: {redacted_args}\n" f"{error_line}"
        )

        # 整体预算约束：加上本段与段间换行后若超预算则停止并加末尾说明。
        if total_chars + len(section) + 1 > budget:
            truncated = True
            break
        sections.append(section)
        total_chars += len(section) + 1

    body = "\n".join(sections)
    if truncated:
        truncation_note = (
            "\n[truncated] Further invalid tool calls omitted due to length budget; "
            "fix the listed calls first and retry."
        )
        # 整条丢弃而非字符切片：保证每个已输出条目字段完整（计划 §6.2）。
        # 从后往前逐个丢弃 section，直至 header + 分隔符 + body + 说明 整体 ≤ 预算。
        while sections:
            candidate = "\n".join(sections) + truncation_note
            if len(header) + 2 + len(candidate) <= budget:
                break
            sections.pop()
        body = "\n".join(sections) + truncation_note

    return f"{header}\n\n{body}".rstrip()


class ToolCallLifecycleManager(BaseModel):
    """工具调用生命周期的 LangGraph state 与事件发射门面。

    state 中只保留 ``calls``，键为 ``tool_call_id``，值为可序列化记录。所有状态方法都
    返回深拷贝后的新 manager，避免节点继续持有旧快照；调用节点必须将返回值放入返回的
    state patch_write。事件发射和模型上下文写回是方法的运行期副作用，依赖从当前 LangGraph
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

    @property
    def invalid_count(self) -> int:
        return sum(
            1 for record in self.calls.values() if record.invalid_detail is not None
        )

    @property
    def has_call(self) -> bool:
        return len(self.calls) > 0

    @property
    def invalid_tools(self) -> list[ToolCallLifecycleRecord]:
        return [record for record in self.calls.values() if record.invalid_detail is not None]

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

    def classify(
        self,
        *,
        task_id: int,
        run_id: int,
        step_id: str,
        tool_calls: list[ToolCall],
        invalid_tool_calls: list[dict[str, Any]],
    ) -> ToolCallLifecycleManager:
        """把模型输出拆解为生命周期记录：合法调用置 running，命中非法 id 的调用挂 invalid_detail。

        判定契约改为按 id 对齐（不再使用基于工具名的名称匹配）：

        - 以 ``invalid_tool_calls`` 的 ``id`` 建立索引；仅携带 ``id`` 的非法调用可被对齐，
          缺失 ``id`` 的视为解析噪声，仅记 warning、不建记录、不阻塞；
        - 合法（工具名已注册且参数已解析）且 id 未命中非法集合的调用经 ``begin`` 置为
          ``running`` 并写入完整参数；
        - id 命中非法集合的调用（无论是否同时出现在合法 ``tool_calls`` 中）不进入 running，
          而是挂载 ``invalid_detail``（原始 invalid_tool_call 的 name/args/error）并维持
          ``pending``，供 observe 节点统一收口并构造修复提示；命中但尚无 lifecycle 记录的
          非法调用合成一条 ``pending`` 记录，维持可修复语义。

        参数:
            task_id, run_id, step_id: 事件定位三元组。
            tool_calls: 模型解析成功的工具调用（``ai_message.tool_calls``）。
            invalid_tool_calls: 模型未解析成功的工具调用（``ai_message.invalid_tool_calls``）。

        返回:
            更新后的 manager（合法调用 running、命中非法的调用 pending 并带 invalid_detail）。
        """

        invalid_by_id = {str(itc["id"]): itc for itc in invalid_tool_calls if itc.get("id")}
        if not invalid_by_id:
            return self.begin(
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                tool_calls=tool_calls,
            )
        # 合法调用中 id 命中非法集合的，不应进入 running，留给下方挂 invalid_detail。
        valid_tool_calls = [tc for tc in tool_calls if tc.call_id not in invalid_by_id]
        updated = self.begin(
            task_id=task_id,
            run_id=run_id,
            step_id=step_id,
            tool_calls=valid_tool_calls,
        )
        for itc_id, invalid_tc in invalid_by_id.items():
            detail: dict[str, object] = {
                "name": invalid_tc.get("name"),
                "args": invalid_tc.get("args"),
                "error": invalid_tc.get("error"),
            }
            existing = updated.calls.get(itc_id)
            if existing is not None:
                existing.invalid_detail = detail
                # 非法调用从未真正执行：若 begin 已置 running 则回退 pending，
                # 使其进入 observe 的非法结算分支而非被当作合法结果分发。
                if existing.status == "running":
                    existing.status = "pending"
                continue
            # 流式期未建对应条目、但 id 已知的非法调用：合成 pending 记录，
            # 使其经 observe 统一结算为 failed 并注入修复提示。
            tool_name = invalid_tc.get("name")
            presentation = (
                updated._presentation_for(tool_name)
                if isinstance(tool_name, str) and tool_name
                else {}
            )
            updated.calls[itc_id] = ToolCallLifecycleRecord(
                tool_call_id=itc_id,
                tool_name=tool_name or "",
                presentation=presentation,
                invalid_detail=detail,
            )
        # 缺失 id 的非法调用无法与生命周期对齐，视为解析噪声忽略。
        unmatched = [itc for itc in invalid_tool_calls if not itc.get("id")]
        if unmatched:
            log.warning(
                "lifecycle_invalid_tool_call_no_id",
                extra={
                    "msg": "非法工具调用缺少 id，无法与生命周期对齐，视为解析噪声忽略",
                    "data": {"step_id": step_id, "count": len(unmatched)},
                },
            )
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

    def fail_invalid(
        self,
        *,
        task_id: int,
        run_id: int,
        step_id: str,
        call_id: str,
        status_hint: str,
    ) -> ToolCallLifecycleManager:
        """收口参数非法的调用：置 failed 并发终态事件，但不写回模型上下文 ToolMessage。

        参数非法的调用从未真正执行，不应产生 ``ToolMessage`` 与合法调用配对；本方法只补发
        终态 ``tool_call_status_changed`` 事件以关闭前端 pending part（``pending`` ->
        ``failed``）。修复提示由 observe 节点经 ``SystemMessage`` 注入，不在此写模型消息。

        参数:
            task_id, run_id, step_id: 事件定位三元组。
            call_id: 待收口的非法调用 id。
            status_hint: 面向前端的短提示（如「参数无效」）。

        返回:
            更新后的 manager。
        """

        updated = self._copy()
        record = updated.calls.get(call_id)
        if record is None or record.status != "pending":
            return updated
        updated._emit_status(
            task_id=task_id,
            run_id=run_id,
            step_id=step_id,
            call_id=call_id,
            to_status="failed",
            error=status_hint,
        )
        updated.calls[call_id].status = "failed"
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
        transport_metadata = TransportMetadata(
            status=event_status,
            display_data=result_display_data,
            error=status_hint,
        )
        created = runtime_context.add_message(
            operations.to_tool_model_message(observation),
            transport_metadata=transport_metadata,
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
