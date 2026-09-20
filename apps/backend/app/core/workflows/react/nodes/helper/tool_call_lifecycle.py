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
from typing import Any, Literal

from langgraph.config import get_stream_writer
from pydantic import BaseModel, ConfigDict, Field

from app.assistant_transport.event import ToolCallCreatedEvent, ToolCallStatusChangedEvent
from app.config.logging.logger import log
from app.core.context.runtime_context_manager import RuntimeContextManager
from app.core.tools.schemas import ToolCall, ToolObservation
from app.core.tools.tool_execute.tool_terminal_projection import (
    normalize_display_data,
    terminal_error_hint,
    terminal_status,
)
from app.core.workflows.react.nodes.helper.common import _runtime_config, _runtime_context
from app.core.workflows.workflow_operations import WorkflowOperations
from app.models.conversation_task_context import TransportMetadata
from app.models.enums.tool_call_status import ToolCallEventStatus

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
    """把观察摘要的 ``status`` 映射为事件契约的终态状态。

    映射规则收口在 ``app.core.tools.tool_execute.tool_terminal_projection``：执行层的提前
    投影与本结算兜底必须给出同一终态，故本函数只做委托，不维护第二份映射。

    参数:
        status: ``ToolObservation.status``（``success`` / ``cancelled`` / 其它错误态）。

    返回:
        ``completed`` / ``cancelled``；其余一律映射为 ``failed``。

    副作用:
        无。
    """

    return terminal_status(status)


def _ui_data(summary: dict[str, Any]) -> dict[str, object] | None:
    """取出终态事件需要更新的 ``display_data``（深拷贝，避免共享摘要结构）。

    形状判定、深拷贝与「载荷被丢弃」的日志留痕都收口在
    ``app.core.tools.tool_execute.tool_terminal_projection`` 的 ``normalize_display_data``：
    提前投影与结算兜底必须对畸形展示数据给出同一种降级与同一种可观测性，故本函数只做摘要
    适配与委托。摘要自带 ``tool_call_id``，作为丢弃日志的定位标识传入。

    参数:
        summary: 单条观察摘要。

    返回:
        摘要中的 ``display_data`` 深拷贝；缺失、不是映射或不可深拷贝时返回 ``None``
        （调用方据此跳过展示字段更新，而不是伪造载荷）。

    副作用:
        委托调用：展示数据存在但归一失败时写 `warning` 级 ``tool_display_data_dropped``。
    """

    return normalize_display_data(
        summary.get("display_data"),
        tool_call_id=str(summary.get("tool_call_id") or ""),
    )


def _ui_error(summary: dict[str, Any], event_status: str) -> str | None:
    """生成事件层的短错误提示（完整诊断保留在模型消息里）。

    提示选择规则收口在 ``app.core.tools.tool_execute.tool_terminal_projection``：执行层的提前
    投影与本结算兜底必须给出同一提示，故本函数只做摘要适配与委托。行为与既有实现一致：
    取消返回「已取消」；失败优先返回后端分类映射的 ``display_data.status_hint``（含空串），
    缺失时回退「执行失败」；``completed`` 返回 ``None``。

    参数:
        summary: 单条观察摘要。
        event_status: 事件终态（``completed`` / ``failed`` / ``cancelled``）。

    返回:
        面向前端的短提示；``completed`` 时返回 ``None``。

    副作用:
        无。
    """

    return terminal_error_hint(event_status, summary.get("display_data"))


def _summary_to_observation(summary: dict[str, Any]) -> ToolObservation:
    """把观察摘要转回 ``ToolObservation``，供模型上下文写入 ToolMessage。

    摘要键名与执行层观察字段一致（``tool_call_id`` 等），由 ``tools`` 节点对
    ``ToolObservation`` 的 ``dataclasses.asdict`` 投影产出。

    参数:
        summary: 单条观察摘要。

    返回:
        与摘要等价的 ``ToolObservation``（``display_data`` 已深拷贝）。

    异常:
        KeyError: 摘要缺少必需键（``tool_name`` / ``status`` / ``content`` / ``error`` /
            ``reason`` / ``retryable`` / ``tool_call_id``）。本函数不做兜底，异常由
            ``observe`` 节点向上冒泡为 workflow 执行异常。

    副作用:
        无（仅字段映射与深拷贝）。
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
    - 每个 repair 条目输出：``## <tool_name>`` + ``name`` / ``args`` 预览（截断到
      ``INVALID_TOOL_ARGS_PREVIEW_CHARS``、超出加 ``...[truncated]``）/ ``error``（若有）。
    - 整体字符预算受 ``INVALID_TOOL_CALL_TOTAL_BUDGET_CHARS`` 约束：逐条拼接，累计超预算即
      停止追加并加末尾截断说明；随后从后往前**整条**丢弃已输出条目（而不是字符切片），
      直至 header 与说明也在预算内，保证保留的每条仍含可定位的 ``tool_name`` 与 ``error``。

    参数:
        repair_datas: 待修复的非法调用明细列表，每项形如
            ``{"tool_name": <命中工具名>,
            "invalid_tool_call": <LangChain invalid_tool_call>}``。

    返回:
        结构化英文提示 ``str``，可直接包装为 ``SystemMessage`` 注入模型上下文。

    异常:
        无（对所有字段做 ``get`` / ``str`` 容错，解析失败的非 JSON 片段也能安全处理）。

    副作用:
        无（只读入参；预览截断为纯函数式处理，不改外部状态）。
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

        args_preview = str(invalid_tc.get("args", ""))
        if len(args_preview) > INVALID_TOOL_ARGS_PREVIEW_CHARS:
            args_preview = args_preview[:INVALID_TOOL_ARGS_PREVIEW_CHARS] + "...[truncated]"

        error = invalid_tc.get("error")
        error_line = f"error: {error}\n" if error else ""

        section = (
            f"## {tool_name}\n" f"name: {tool_name}\n" f"args: {args_preview}\n" f"{error_line}"
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
        """返回已挂 ``invalid_detail`` 的非法调用数量。"""

        return sum(1 for record in self.calls.values() if record.invalid_detail is not None)

    @property
    def has_call(self) -> bool:
        """返回本快照是否登记了任意工具调用（合法或非法）。"""

        return len(self.calls) > 0

    @property
    def invalid_tools(self) -> list[ToolCallLifecycleRecord]:
        """返回全部挂 ``invalid_detail`` 的非法调用记录。"""

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

        参数:
            task_id, run_id, step_id: 事件定位三元组。
            raw_tool_calls: 原始工具调用字典列表（每项含 ``id`` / ``name``）。

        返回:
            追加了本次新建记录的新 manager；未建立任何记录时返回等值的新快照。

        副作用:
            经 stream writer 为每条新建记录发出 ``ToolCallCreatedEvent``（该事件本身把前端
            part 初始化为 ``pending``，所以不再单独发射 pending 状态事件）。
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
        """发射一条工具调用状态迁移事件。

        参数:
            task_id, run_id, step_id: 事件定位三元组。
            call_id: 目标工具调用 id。
            to_status: 目标状态（``running`` / ``completed`` / ``failed`` / ``cancelled``）。
            args: 可选，完整参数（仅合法调用迁移到 ``running`` 时携带）。
            error: 可选，面向前端的短错误提示。
            display_data: 可选，终态事件的展示数据。

        返回:
            无。

        副作用:
            经 stream writer 发出 ``ToolCallStatusChangedEvent``；迁移合法性由 snapshot
            projector 校验（非法迁移只记日志、不杀死执行）。
        """

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

    def _emit_status_safe(
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
        """发射状态迁移事件；失败只降级记日志，绝不阻断调用方的状态迁移。

        状态事件是「通知前端」的旁路：它失败不能让已经确定的状态迁移半途而废，否则快照会出现
        「事件已发、状态未落」的不一致（非法调用会退回「pending 且无终态」）。本方法把该降级
        收口在一处，供 ``begin`` / ``cancel`` / ``_fail_invalid`` / ``settle`` 共用。

        参数:
            task_id, run_id, step_id: 事件定位三元组。
            call_id: 目标工具调用 id。
            to_status: 目标状态（``running`` / ``completed`` / ``failed`` / ``cancelled``）。
            args: 可选，完整参数（仅合法调用迁移到 ``running`` 时携带）。
            error: 可选，面向前端的短错误提示。
            display_data: 可选，终态事件的展示数据。

        返回:
            无。

        异常:
            无。``BaseException``（如 ``KeyboardInterrupt``）仍照常上抛，不被本方法吞掉。

        副作用:
            成功时经 :meth:`_emit_status` 发出一次 ``ToolCallStatusChangedEvent``；失败时写
            ``tool_terminal_event_failed`` ERROR 日志（含 ``task_id`` / ``run_id`` /
            ``tool_call_id`` / ``status`` 等定位字段）后返回，调用方的状态迁移照常完成。
        """

        try:
            self._emit_status(
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                call_id=call_id,
                to_status=to_status,
                args=args,
                error=error,
                display_data=display_data,
            )
        except Exception:
            log.exception(
                "tool_terminal_event_failed",
                extra={
                    "msg": "工具调用状态事件发送失败并被降级",
                    "data": {
                        "task_id": task_id,
                        "run_id": run_id,
                        "tool_call_id": call_id,
                        "status": to_status,
                    },
                },
            )

    def begin(
        self,
        *,
        task_id: int,
        run_id: int,
        step_id: str,
        tool_calls: list[ToolCall],
    ) -> ToolCallLifecycleManager:
        """把 pending 调用迁移到 running，并写入完整解析后的参数。

        未登记过的 ``call_id`` 先经 :meth:`create` 补建记录（流式期未捕获、聚合后才出现的
        调用），仅 ``pending`` 记录会被迁移；已是 ``running`` 或终态的记录跳过。

        参数:
            task_id, run_id, step_id: 事件定位三元组。
            tool_calls: 模型解析成功的工具调用（``ai_message.tool_calls``）。

        返回:
            更新后的 manager（**恒为新快照**）：被迁移的调用状态为 ``running`` 且带完整 ``args``。

        副作用:
            每条迁移经 stream writer 发出一次 ``running`` 状态事件；事件发送失败只记
            ``tool_terminal_event_failed`` 并降级继续，状态迁移照常完成。
        """

        # 与 ``cancel`` / ``fail_invalid_tools`` 同口径：恒以新快照起手，返回值身份可预期；
        # 起手即复制后，迁移过程直接在该快照上推进即可，无需逐步复制。
        updated = self._copy()
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
            updated._emit_status_safe(
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                call_id=tool_call.call_id,
                to_status="running",
                args=tool_call.arguments,
            )
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
        tool_calls: list[ToolCall] | None = None,
    ) -> ToolCallLifecycleManager:
        """把尚未结束的调用迁移到 cancelled，并为每条发出终态事件。

        收口判据是状态本身：只处理 ``pending`` / ``running`` 记录，已终态的跳过，因此可重复
        调用；适用于「流式期 ``create`` 已把调用投影给前端、但该调用不会真正执行」的场景，
        避免前端留下悬空的「执行中」part。

        注意：当前 workflow 生产路径没有调用方——协作取消由 ``model_node`` 经 ``interrupt``
        中断图，工具侧取消由执行层的取消检查产生 ``cancelled`` 观察。本方法与其单测作为
        取消收口能力保留，接入新的调用点时需同步确认图路由。

        参数:
            task_id, run_id, step_id: 事件定位三元组。
            tool_calls: 待收口的调用；为 ``None`` 时收口本 manager 当前**全部** ``pending`` /
                ``running`` 调用（取消场景通常无需先枚举，因为收口判据就是状态本身）。

        返回:
            更新后的 manager；已终态的调用不受影响。

        副作用:
            经 stream writer 为每条被收口的调用发出一次 ``cancelled`` 终态事件；事件发送失败
            只记 ``tool_terminal_event_failed`` 并降级继续，状态迁移照常完成。
        """

        updated = self._copy()
        call_ids = (
            list(updated.calls)
            if tool_calls is None
            else [tool_call.call_id for tool_call in tool_calls]
        )
        for call_id in call_ids:
            record = updated.calls.get(call_id)
            if record is None or record.status not in {"pending", "running"}:
                continue
            updated._emit_status_safe(
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                call_id=call_id,
                to_status="cancelled",
            )
            updated.calls[call_id].status = "cancelled"
        return updated

    def fail_invalid_tools(
        self,
        task_id: int,
        run_id: int,
        step_id: str,
    ) -> tuple[ToolCallLifecycleManager, str | None]:
        """收口全部参数非法的调用，并产出面向模型的修复提示文本。

        本方法是「非法调用批量收口」的唯一入口：逐条把仍为 ``pending`` 的非法调用交给
        :meth:`_fail_invalid` 置 ``failed`` 并补发终态事件，同时收集可修复明细。

        ``_fail_invalid`` 遵循本类的 copy-on-write 约定（在 ``_copy()`` 出的新快照上迁移状态），
        因此**必须把它的返回值逐次累积并返回给调用方**；丢弃返回值会让 ``self`` 上的记录永远停在
        ``pending``，而调用方写回 graph state 的又是这份未迁移的快照，导致后续按状态判定的逻辑
        （观察节点结算、快照重建、二次收口）读到错误的生命周期。

        参数:
            task_id, run_id, step_id: 事件定位三元组。

        返回:
            ``(更新后的 manager, 修复提示或 None)``。manager **恒为新的快照**（与本类其它迁移
            方法一致，避免调用方依赖「有时是新对象、有时是原对象」的隐式差异），调用方**必须**
            把它写回 graph state；提示为 ``None`` 表示本批没有可修复的非法调用。

        异常:
            无。单条收口内部不做额外校验，状态非 ``pending`` 的调用按原样跳过。

        副作用:
            对每条被收口的调用经 stream writer 发出一次 ``failed`` 终态事件；事件发送失败只记
            ``tool_terminal_event_failed`` 并降级继续，不写模型上下文（非法调用从未执行，不产生
            ``ToolMessage``）。
        """

        # 与 ``settle`` / ``cancel_pending`` 同口径：无论本批是否有待收口调用，都交出新的快照，
        # 让调用方拿到的生命周期对象身份是可预期的。
        updated = self._copy()
        repair_datas: list[dict[str, Any]] = []
        for record in self.invalid_tools:
            if record.status != "pending":
                continue
            updated = updated._fail_invalid(
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                call_id=record.tool_call_id,
                status_hint="参数无效",
            )
            repair_datas.append(
                {"tool_name": record.tool_name, "invalid_tool_call": record.invalid_detail}
            )

        # 修复提示必须排在全部 ToolMessage 之后注入，故由调用方经延迟队列下发。
        if not repair_datas:
            return updated, None
        return updated, build_invalid_tool_call_repair_message(repair_datas)

    def _fail_invalid(
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
            更新后的 manager；目标记录不存在或不是 ``pending`` 时按原样返回新快照。

        副作用:
            经 stream writer 发出一次 ``failed`` 终态事件（``error`` 即 ``status_hint``）；
            事件发送失败只记 ``tool_terminal_event_failed`` 并降级继续，状态迁移照常完成。
        """

        updated = self._copy()
        record = updated.calls.get(call_id)
        if record is None or record.status != "pending":
            return updated
        updated._emit_status_safe(
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
        """结算单条观察：写回模型上下文、更新 state 并发出终态事件。

        顺序是刻意的：先把 ``ToolMessage`` 写入 canonical context，再发终态 Transport 事件，
        使 projector 不会发布一个无法从 context 重建的终态工具状态。

        **已知且有意的例外：全部终态都可能早于本方法**。执行层在工具跑完（process 路径为进程
        强杀与输出排空完成）之后，就把 ``completed`` / ``failed`` / ``cancelled`` 投影进进程内
        snapshot（执行出口投影，见 ``tool_terminal_projection.project_tool_terminal_state``），
        使用户不必等整批工具跑完就能看到结果。该例外不会产生无法重建的状态：快照是进程内
        ephemeral working copy，进程终止后由冷重建从数据库重建，而冷重建对「没有
        ``ToolMessage`` 行的 tool-call part」本来就默认投影为 ``cancelled``
        （``ConversationTaskStateRebuilder.build_pair_tool_part``），与「结果丢失且 run 已收敛」
        的事实一致；代价是进程在写 ``ToolMessage`` 之前终止时，该 part 会从 ``completed`` /
        ``failed`` 回退为 ``cancelled``。本方法随后写 ``ToolMessage`` 并再发一次同值终态，投影按
        自迁移幂等吸收。

        参数:
            task_id, run_id, step_id: 事件定位三元组。
            summary: 单条工具观察摘要（``tools`` 节点的 ``dataclasses.asdict`` 投影）。

        返回:
            ``(更新后的 manager, 事件终态)``；调用已处于终态时原样返回新快照与既有终态，
            不重复写上下文、不重复发事件。

        异常:
            KeyError: 摘要缺少必需字段（见 ``_summary_to_observation``）；本方法不兜底。

        副作用:
            写一条 ``ToolMessage`` 到 ``RuntimeContextManager``；``add_message`` 返回 ``False``
            （该消息已存在）时直接返回、不再发事件；成功持久化后经 stream writer 发出终态事件，
            事件发送失败只记 ``tool_terminal_event_failed`` 并降级继续。
        """

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
        # 刻意先落库上下文、再发终态事件：否则 projector 可能发布一个无法从 context
        # 重建的终态工具状态。
        updated._emit_status_safe(
            task_id=task_id,
            run_id=run_id,
            step_id=step_id,
            call_id=call_id,
            to_status=event_status,
            error=status_hint,
            display_data=copy.deepcopy(result_display_data),
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
        """结算一批观察摘要，返回错误计数和更新后的 lifecycle。

        连续失败计数规则：``completed`` 清零、``failed`` 累加（同时累加本批 ``error_count``）、
        ``cancelled`` 既不计也不清零；``inherited_error_count`` 是上一批留下的计数。

        参数:
            task_id, run_id, step_id: 事件定位三元组。
            summaries: 本批观察摘要列表。
            inherited_error_count: 继承自 state 的连续失败计数。

        返回:
            ``SettlementResult``：更新后的连续失败计数、本批失败条数与 lifecycle 快照。

        异常:
            KeyError: 某条摘要缺少必需字段（见 :meth:`settle`）；本方法不兜底。

        副作用:
            逐条经 :meth:`settle` 写模型上下文并发终态事件；结束时记一条
            ``observe_node_dispatch_completed`` 汇总日志。
        """

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
