"""工具调用生命周期状态与事件发射。

本模块承载 ``ToolCallLifecycleManager``：LangGraph state 中的可序列化生命周期快照，负责登记本步
模型请求的工具调用、迁移状态并发出工具调用生命周期事件。不可序列化的运行期依赖
（``RuntimeConfig`` / ``RuntimeContextManager`` / stream writer）不进 checkpoint，方法执行时从当前
graph config 取得。

状态链为 ``pending -> running -> completed/failed/cancelled``；``settle`` 允许 ``pending`` 不经
``running`` 直接落终态。创建事件已把 Transport part 初始化为 ``pending``，故不再单独发射
``pending`` 状态事件。状态方法返回新快照，调用节点必须把返回值写回 graph state；返回契约详见
``ToolCallLifecycleManager`` 类 docstring。
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Mapping
from typing import Any, Literal

from langchain_core.messages import InvalidToolCall
from langchain_core.messages.tool import ToolCall
from langgraph.config import get_stream_writer
from pydantic import BaseModel, ConfigDict, Field

from app.assistant_transport.event import ToolCallCreatedEvent, ToolCallStatusChangedEvent
from app.config.logging.logger import log
from app.core.context.runtime_context_manager import RuntimeContextManager
from app.core.tools.schemas import ToolObservation
from app.core.tools.tool_execute.tool_terminal_projection import (
    normalize_display_data,
    terminal_error_hint,
    terminal_status,
)
from app.core.workflows.react.node_helper.common import _runtime_config, _runtime_context
from app.core.workflows.workflow_operations import WorkflowOperations
from app.models.conversation_task_context import TransportMetadata
from app.models.enums.tool_call_status import ToolCallEventStatus


class ToolCallLifecycleRecord(BaseModel):
    """单次工具调用的可序列化生命周期记录。

    承载调用身份（``tool_call_id`` / ``tool_name``）、状态（默认 ``pending``）、已解析参数与静态
    展示声明 ``presentation``（``create`` / ``classify`` 的 ``allowed`` 桶写入，``settle`` 对缺失
    记录的补建按工具名写入）。

    异常:
        pydantic.ValidationError: 字段不满足契约（``tool_call_id`` / ``tool_name`` 为空，或出现
            ``extra="forbid"`` 未声明的字段）时抛出。
    """

    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    status: ToolCallEventStatus = "pending"
    args: dict[str, object] = Field(default_factory=dict)
    presentation: dict[str, object] = Field(default_factory=dict)


@dataclasses.dataclass
class SettlementResult:
    """一批观察结算后的计数与生命周期快照。

    ``tool_error_count`` 是连续失败计数，``error_count`` 是本批「不可重试失败」条数；``lifecycle``
    不参与相等比较，兼容调用方只比较计数的既有契约，observe 节点用它把结算后的 manager 写回
    LangGraph state。

    异常:
        无（``dataclasses.dataclass`` 不做字段校验）。
    """

    tool_error_count: int
    error_count: int
    # settle_batch / settle 始终返回非空的 manager（未命中调用会即时补建），无需 Optional。
    lifecycle: ToolCallLifecycleManager = dataclasses.field(compare=False, repr=False)


def _event_status(status: str) -> Literal["completed", "failed", "cancelled"]:
    """把 ``ToolObservation.status`` 映射为事件契约的终态状态。

    映射规则收口在 ``tool_terminal_projection.terminal_status``：执行层提前投影与本结算兜底必须
    给出同一终态，故本函数只做委托，不维护第二份映射。

    参数:
        status: ``ToolObservation.status``（``success`` / ``cancelled`` / 其它错误态）。

    返回:
        ``completed`` / ``cancelled``；其余一律 ``failed``。

    异常:
        无。

    副作用:
        无。
    """

    return terminal_status(status)


def _record_args(raw_args: object) -> dict[str, object]:
    """把原始调用参数归一为记录字段可接受的结构化参数。

    ``create`` 收到的流式分片参数与 ``classify`` 收到的未解析调用参数都可能是原始 JSON 片段
    （``str``）而不是字典，而 :class:`ToolCallLifecycleRecord.args` 只承载已解析参数。映射直接
    取其普通 ``dict`` 拷贝；**非映射保留原始值并放入保留键 ``Invaild_args``**，使调用进入执行链
    后，修订版 ``ai_message.tool_calls`` 里仍带着模型自己发错的原始内容——这是刻意的：
    参数写错时必须让模型看见错在哪，而不是把参数抹成空。参数合法性仍由执行层
    ``ToolAccessGate`` 校验并产出可读拒绝（如缺少必需参数），该拒绝随错误观察一并回给模型。

    参数:
        raw_args: 原始 ``args``（字典、JSON 片段 ``str``、``None`` 或其他对象）。

    返回:
        映射时为其普通 ``dict`` 拷贝；否则为 ``{"Invaild_args": <原始值>}``（键名拼写照旧保留，
        它已是下发模型与落库的对外形态）。

    异常:
        无。

    副作用:
        无。
    """

    return dict(raw_args) if isinstance(raw_args, Mapping) else {"Invaild_args":raw_args}


def _ui_data(summary: dict[str, Any]) -> dict[str, object] | None:
    """取出终态事件需要更新的 ``display_data``（深拷贝，避免共享摘要结构）。

    形状判定、深拷贝与「载荷被丢弃」留痕都收口在
    ``tool_terminal_projection.normalize_display_data``：提前投影与结算兜底必须对畸形展示数据给出
    同一种降级与同一种可观测性，故本函数只做摘要适配与委托。摘要自带的 ``tool_call_id`` 作为丢弃
    日志的定位标识传入。

    参数:
        summary: 单条观察摘要。

    返回:
        摘要 ``display_data`` 的深拷贝；缺失、不是映射或不可深拷贝时返回 ``None``（调用方据此
        跳过展示字段更新，而不是伪造载荷）。

    异常:
        无（深拷贝异常由被委托方吞掉）。

    副作用:
        委托调用：展示数据存在但归一失败时写 warning ``tool_display_data_dropped``。
    """

    return normalize_display_data(
        summary.get("display_data"),
        tool_call_id=str(summary.get("tool_call_id") or ""),
    )


def _ui_error(summary: dict[str, Any], event_status: str) -> str | None:
    """生成事件层的短错误提示（完整诊断保留在模型消息里）。

    提示选择规则收口在 ``tool_terminal_projection.terminal_error_hint``：提前投影与结算兜底必须
    给出同一提示，故本函数只做摘要适配与委托。行为：取消返回「已取消」；失败优先返回后端分类
    映射的 ``display_data.status_hint``（含空串），非映射或缺少字符串提示时回退「执行失败」；
    ``completed`` 返回 ``None``。

    参数:
        summary: 单条观察摘要。
        event_status: 事件终态（``completed`` / ``failed`` / ``cancelled``）。

    返回:
        面向前端的短提示；``completed`` 时返回 ``None``。

    异常:
        无。

    副作用:
        无。
    """

    return terminal_error_hint(event_status, summary.get("display_data"))


def _summary_to_observation(summary: dict[str, Any]) -> ToolObservation:
    """把观察摘要转回 ``ToolObservation``，供模型上下文写入 ToolMessage。

    摘要键名与执行层观察字段一致（``tool_call_id`` 等），由 ``tools`` 节点对 ``ToolObservation``
    做 ``dataclasses.asdict`` 投影产出。

    参数:
        summary: 单条观察摘要。

    返回:
        与摘要等价的 ``ToolObservation``（``display_data`` 经 :func:`_ui_data` 归一为深拷贝或
        ``None``）。

    异常:
        KeyError: 摘要缺少必需键（``tool_name`` / ``status`` / ``content`` / ``error`` /
            ``reason`` / ``retryable`` / ``tool_call_id``）。本函数不做兜底，异常由
            ``observe`` 节点向上冒泡为 workflow 执行异常。

    副作用:
        经 :func:`_ui_data` 委派：展示数据存在但归一失败时写 warning ``tool_display_data_dropped``；
        自身不写其他状态。
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

    可序列化 state 字段（类体声明）为三条：``valid_calls``（身份可用且未被本轮 ``allows_tools``
    拦截的调用，含工具名未注册者——那类调用仍交给执行层判定）、``blocked_calls``（已注册但本轮未
    放行：不发创建事件、前端无对应 part，但同样送入执行层由 ``ToolAccessGate`` 拒绝，其观察用于
    闭合模型协议）、``allows_tools``（本 Run 允许执行的工具名快照）。

    归属由 :meth:`_resolve_call_bucket` 在 ``create`` / ``classify`` 阶段一次性裁定；``classify``
    整体重建 ``valid_calls`` / ``blocked_calls``，``settle`` 对缺失记录即时补建 ``pending``，其余
    状态方法在既有记录上演化状态。状态方法恒返回深拷贝的新快照（``settle`` 返回
    ``(manager, 终态)``，``settle_batch`` 返回 ``SettlementResult``），调用节点必须把返回值写入
    state patch。事件发射与模型上下文写回是运行期副作用，依赖取自当前 graph config，不经 Pydantic
    / LangGraph 序列化。

    异常:
        pydantic.ValidationError: manager state 或事件字段不满足契约时抛出。
        KeyError: 观察摘要缺少模型上下文所需字段时由上游契约错误触发。
    """

    model_config = ConfigDict(extra="forbid")

    # 身份可用（id 非空且 name 为 str）且未被本轮 allows_tools 拦截的调用。参数错误与未注册工具名
    # 不在此拦截，交给执行层 ToolAccessGate 判定并产出错误观察。
    valid_calls: dict[str, ToolCallLifecycleRecord] = Field(default_factory=dict)
    blocked_calls: dict[str, ToolCallLifecycleRecord] = Field(default_factory=dict)
    allows_tools: tuple[str, ...] = ()

    def _copy(self) -> ToolCallLifecycleManager:
        """深拷贝本快照，供状态迁移以新对象返回（调用方必须接住返回值）。

        返回:
            与本快照等值但独立的 manager；迁移不得就地改写入参快照。

        异常:
            无。

        副作用:
            无（仅内存复制）。
        """

        return self.model_copy(deep=True)

    @property
    def has_call(self) -> bool:
        """判断本快照是否登记了任意工具调用。

        返回:
            ``valid_calls`` 或 ``blocked_calls`` 非空时为 ``True``，两者皆空为 ``False``。

        异常:
            无。
        """

        return bool(self.valid_calls) or bool(self.blocked_calls)

    @property
    def valid_tools(self) -> list[ToolCallLifecycleRecord]:
        """列出 ``valid_calls`` 全集（可执行调用记录，含工具名未注册者）。

        返回:
            新构造的记录列表，元素为快照内记录本身。

        异常:
            无。
        """

        return list(self.valid_calls.values())

    @property
    def blocked_tool_calls(self) -> list[ToolCallLifecycleRecord]:
        """列出 ``blocked_calls`` 全集（已注册但本轮未放行、不执行的隐藏闭合调用）。

        返回:
            新构造的记录列表，元素为快照内记录本身。

        异常:
            无。
        """

        return list(self.blocked_calls.values())

    @staticmethod
    def _valid_tool_name(tool_name: object) -> bool:
        """判断工具名是否为当前运行时注册的工具。

        参数:
            tool_name: 待判定的对象（可能缺失、非 ``str`` 或为空串）。

        返回:
            非空 ``str`` 且存在于 ``operations.all_vaild_tools`` 时为 ``True``，否则 ``False``。

        异常:
            RuntimeError / KeyError / TypeError: 仅可在 graph 运行上下文内调用
                （:func:`_runtime_config` 取不到配置时抛出）。

        副作用:
            只读运行时配置（``_runtime_config().operations``）；无写入。
        """

        if not isinstance(tool_name, str) or not tool_name:
            return False
        operations: WorkflowOperations = _runtime_config().operations
        return tool_name in {tool.name for tool in operations.all_vaild_tools or []}

    @staticmethod
    def _presentation_for(tool_name: str) -> dict[str, object]:
        """读取工具的静态展示声明（``ToolDefinition.display`` 序列化结果）。

        参数:
            tool_name: 工具名。

        返回:
            同名且声明了 ``display`` 的工具的声明字典（每次调用新构造）；未注册或无 ``display``
            时返回空 ``dict``。

        异常:
            RuntimeError / KeyError / TypeError: 仅可在 graph 运行上下文内调用
                （:func:`_runtime_config` 取不到配置时抛出）。

        副作用:
            只读运行时配置；无写入。
        """

        operations: WorkflowOperations = _runtime_config().operations
        for definition in operations.all_vaild_tools:
            if definition.name == tool_name and definition.display is not None:
                return definition.display.to_dict()
        return {}

    def _resolve_call_bucket(
            self, call_id: object, tool_name: object
    ) -> Literal["allowed", "blocked", "ignore_no_id", "ignore_bad_name", "invaild_tool_name"]:
        """裁决一条原始工具调用的归属桶，是本类注册 / 白名单判定的唯一裁决点。

        ``create``（流式建记录）与 ``classify``（已解析 + 未解析两条输入）都调用本方法，使
        「``id`` 可用性」「``name`` 是否已注册」「``name`` 是否在本轮 ``allows_tools``」三条规则
        在全类只有一处定义。判定按序：

        - ``id`` 非 ``str`` 或空串 -> ``ignore_no_id``（解析噪声，不入任何集合）；
        - ``name`` 非 ``str`` -> ``ignore_bad_name``（同上）；
        - ``name`` 已注册但不在 ``allows_tools`` -> ``blocked``（隐藏闭合集合，不执行）；
        - ``name`` 未注册（含空串）-> ``invaild_tool_name``（仍按可执行调用处理，参数与工具名
          合法性交给执行层 ``ToolAccessGate`` 判定）；
        - 其余（已注册且在 ``allows_tools``）-> ``allowed``。

        参数:
            call_id: 原始调用 id（可能缺失、非 ``str`` 或为空）。
            tool_name: 原始调用 name（可能缺失、非 ``str`` 或未注册）。

        返回:
            ``"allowed"`` / ``"blocked"`` / ``"ignore_no_id"`` / ``"ignore_bad_name"`` /
            ``"invaild_tool_name"``（字面量拼写与代码一致）。``ignore_*`` 表示该调用应被调用方
            剔除且不入任何集合；``allowed`` / ``blocked`` 保证 ``tool_name`` 为已注册 ``str``。

        异常:
            RuntimeError / KeyError / TypeError: 仅可在 graph 运行上下文内调用
                （:func:`_runtime_config` 取不到配置时抛出）。

        副作用:
            只读运行时 ``operations.all_vaild_tools``（经 :meth:`_valid_tool_name`）；无写入。
        """

        if not isinstance(call_id, str) or not call_id.strip():
            return "ignore_no_id"
        if not isinstance(tool_name, str) or not tool_name.strip():
            return "ignore_bad_name"
        if self._valid_tool_name(tool_name) and tool_name not in self.allows_tools:
            return "blocked"
        # 身份可用（id 非空、name 为 str）即允许进入执行链；参数与未注册工具名的合法性由执行层
        # ToolAccessGate 判定并产出错误观察，不在此拦截。
        if not self._valid_tool_name(tool_name):
            return "invaild_tool_name"
        return "allowed"

    def create(
            self,
            *,
            task_id: int,
            run_id: int,
            step_id: str,
            raw_tool_calls: list[dict[str, Any]],
    ) -> ToolCallLifecycleManager:
        """为流式期到达的原始工具调用建记录；仅 ``allowed`` 桶发创建事件。

        ``raw_tool_calls`` 可来自单个流式 chunk，一个 chunk 中的多条调用逐条处理。归属由
        :meth:`_resolve_call_bucket` 统一裁决：``ignore_*``（``id`` / ``name`` 尚未到达的流式
        中间态）跳过；``blocked`` 记入 ``blocked_calls``；``invaild_tool_name`` 记入
        ``valid_calls``；``allowed`` 记入 ``valid_calls`` 并发创建事件。已在快照中登记过的 ``id``
        幂等跳过（不重复建记录）。流式分片的 ``args`` 多为未闭合 JSON 片段，经 :func:`_record_args`
        归一为 ``dict``，完整参数由后续 ``classify`` 用聚合后的调用重写。

        参数:
            task_id, run_id, step_id: 事件定位三元组。
            raw_tool_calls: 原始工具调用字典列表（每项含 ``id`` / ``name`` / ``args``）。

        返回:
            追加了本次新建记录的新 manager；未建立任何记录时返回等值的新快照。

        异常:
            pydantic.ValidationError: 记录字段不满足契约（如 ``tool_name`` 为空）时抛出。
            RuntimeError / KeyError / TypeError: 仅可在 graph 运行上下文内调用
                （经 :meth:`_resolve_call_bucket` → :func:`_runtime_config` 取不到配置时抛出）。

        副作用:
            ``allowed`` 桶经 stream writer 发 ``ToolCallCreatedEvent``（该事件把前端 part 初始化
            为 ``pending``，故不再单发 pending 状态事件），并把静态展示声明写入记录；
            ``blocked`` / ``invaild_tool_name`` 桶只落 state，不发事件、不写日志。
        """

        updated = self._copy()
        stream_writer = get_stream_writer()
        for raw_call in raw_tool_calls:
            call_id = raw_call.get("id")
            tool_name = raw_call.get("name")
            bucket = self._resolve_call_bucket(call_id, tool_name)
            if bucket in {"ignore_no_id", "ignore_bad_name"}:
                continue
            if (
                    call_id in updated.valid_calls
                    or call_id in updated.blocked_calls
            ):
                continue
            if bucket == "blocked":
                updated.blocked_calls[call_id] = ToolCallLifecycleRecord(
                    tool_call_id=call_id,
                    tool_name=tool_name,
                    args=_record_args(raw_call.get("args"))
                )
                continue
            if bucket == "invaild_tool_name":
                updated.valid_calls[call_id] = ToolCallLifecycleRecord(
                    tool_call_id=call_id,
                    tool_name=tool_name,
                    args=_record_args(raw_call.get("args"))
                )
                continue
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
            updated.valid_calls[call_id] = ToolCallLifecycleRecord(
                tool_call_id=call_id,
                tool_name=tool_name,
                args=_record_args(raw_call.get("args")),
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
            to_status: 目标状态（``pending`` / ``running`` / ``completed`` / ``failed`` /
                ``cancelled``）。
            args: 可选，完整参数（仅调用迁移到 ``running`` 时携带）；发出前深拷贝。
            error: 可选，面向前端的短错误提示。
            display_data: 可选，终态展示数据（调用方负责拷贝）。

        返回:
            无。

        异常:
            pydantic.ValidationError: 事件字段不满足契约时抛出。
            RuntimeError / KeyError / TypeError: 不在 graph 运行上下文内调用
                （``get_stream_writer`` 取不到 stream writer 时抛出）。

        副作用:
            经 stream writer 发出 ``ToolCallStatusChangedEvent``；迁移合法性由 snapshot projector
            的 ``plan`` 校验，非法迁移最终由 ``WorkflowOperations.process_event`` 记
            ``workflow_event_projector_failed`` 并降级，不中断执行。
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

    def begin(
            self,
            *,
            task_id: int,
            run_id: int,
            step_id: str,
    ) -> ToolCallLifecycleManager:
        """把 ``valid_calls`` 中状态为 ``pending`` 的调用迁移到 ``running``。

        仅迁移状态为 ``pending`` 且工具名当前仍注册的记录；已是 ``running`` / 终态、或工具名已不在
        运行时注册表中的记录跳过。参数直接沿用记录内的 ``args``，无需在此重新解析。

        参数:
            task_id, run_id, step_id: 事件定位三元组。

        返回:
            更新后的 manager（**恒为新快照**）：被迁移的记录状态为 ``running``。

        异常:
            RuntimeError / KeyError / TypeError: 仅可在 graph 运行上下文内调用
                （:meth:`_valid_tool_name` 经 :func:`_runtime_config`、:meth:`_emit_status` 经
                ``get_stream_writer`` 取不到上下文时抛出）。
            pydantic.ValidationError: :meth:`_emit_status` 的事件字段不满足契约时抛出。

        副作用:
            每条迁移经 :meth:`_emit_status` 发出一次 ``running`` 状态事件；事件构造或入队失败直接
            向上抛出，不在本方法内降级。
        """

        # 与 ``cancel`` 同口径：恒以新快照起手，返回值身份可预期；起手即复制后，迁移过程直接在
        # 该快照上推进即可，无需逐步复制。
        updated = self._copy()
        for record in self.valid_calls.values():
            if record is None or record.status != "pending":
                continue
            if not self._valid_tool_name(record.tool_name):
                continue
            updated._emit_status(
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                call_id=record.tool_call_id,
                to_status="running",
                args=record.args,
            )
            updated.valid_calls[record.tool_call_id].status = "running"
        return updated

    def classify(
            self,
            *,
            tool_calls: list[ToolCall],
            invalid_tool_calls: list[InvalidToolCall],
    ) -> ToolCallLifecycleManager:
        """把模型输出拆解为 ``valid_calls`` / ``blocked_calls`` 两条集合。

        遍历 ``tool_calls + invalid_tool_calls``（后者覆盖先出现的同名 ``id``），逐条经
        :meth:`_resolve_call_bucket` 裁决：``blocked`` 进 ``blocked_calls``；``allowed`` 与
        ``invaild_tool_name`` 进 ``valid_calls``（展示声明仅在 ``allowed`` 桶写入，该桶为空
        ``dict``）；``ignore_*`` 静默丢弃。记录状态保持默认 ``pending``——置 ``running`` 由后续
        ``tools`` 节点的 :meth:`begin` 完成。参数经 :func:`_record_args` 归一：未解析调用的 ``args``
        是 JSON 片段字符串，非映射时原始值放入保留键 ``Invaild_args``（该调用仍送执行层，由参数
        校验产出可读拒绝）。

        参数:
            tool_calls: 模型解析成功的工具调用（``ai_message.tool_calls``）。
            invalid_tool_calls: 模型未解析成功的工具调用（``ai_message.invalid_tool_calls``）。

        返回:
            新快照：``valid_calls`` / ``blocked_calls`` 被整体重建（不合并既有记录）。

        异常:
            pydantic.ValidationError: 记录字段不满足契约（如 ``tool_name`` 为空）时抛出。
            RuntimeError / KeyError / TypeError: 仅可在 graph 运行上下文内调用
                （经 :meth:`_resolve_call_bucket` → :func:`_runtime_config` 取不到配置时抛出）。

        副作用:
            无事件、无日志；调用方必须把返回快照写回 state。
        """

        all_calls = tool_calls + invalid_tool_calls

        valid_calls: dict[str, ToolCallLifecycleRecord] = {}
        blocked_calls: dict[str, ToolCallLifecycleRecord] = {}

        for call in all_calls:
            call_id = call.get("id")
            call_name = call.get("name")
            call_args = _record_args(call.get("args"))
            bucket = self._resolve_call_bucket(call_id, call_name)
            if bucket == "blocked":
                blocked_calls[call_id] = ToolCallLifecycleRecord(
                    tool_call_id=call_id,
                    tool_name=call_name,
                    args=call_args,
                )
            elif bucket == "allowed" or bucket == "invaild_tool_name":
                presentation = self._presentation_for(call_name) if bucket == "allowed" else {}
                valid_calls[call_id] = ToolCallLifecycleRecord(
                    tool_call_id=call_id,
                    tool_name=call_name,
                    args=call_args,
                    presentation=presentation,
                )

        updated = self._copy()
        updated.valid_calls = valid_calls
        updated.blocked_calls = blocked_calls
        return updated

    def cancel(
            self,
            *,
            task_id: int,
            run_id: int,
            step_id: str,
    ) -> ToolCallLifecycleManager:
        """把尚未结束的调用迁移到 cancelled，并为每条发出终态事件。

        收口判据是状态本身：只处理仍处于 ``pending`` / ``running`` 的可执行调用记录，已终态的跳过，
        因此可重复调用；适用于「流式期 :meth:`create` 已把调用投影给前端、但该调用不会真正执行」的
        场景，避免前端留下悬空的「执行中」part。含从未发创建事件的未注册工具名调用（那类事件会被
        projector 跳过并记 warning）；``blocked_calls`` 从未发创建事件，不参与收口。

        调用点：``model_node`` 在流式循环内检测到协作取消时调用本方法，随后才 ``interrupt`` 挂起
        节点；顺序不可颠倒（``interrupt`` 之后的语句不会执行）。返回值（copy-on-write 快照）必须
        接住并写回 state，否则同一分支被重复进入时会读到未迁移的旧快照，对已 ``cancelled`` 的调用
        重复发事件。本方法发出的 ``cancelled`` 事件是取消时关闭前端 part 的主通道；冷读重建对没有
        ``ToolMessage`` 行的 part 同样默认 ``cancelled``，两者口径一致。执行层取消检查产生的
        ``cancelled`` 观察不经过本方法。

        参数:
            task_id, run_id, step_id: 事件定位三元组。

        返回:
            更新后的 manager；已终态的调用不受影响。

        异常:
            pydantic.ValidationError: :meth:`_emit_status` 的事件字段不满足契约时抛出。
            RuntimeError / KeyError / TypeError: 不在 graph 运行上下文内调用
                （:meth:`_emit_status` 经 ``get_stream_writer`` 取不到 stream writer 时抛出）。

        副作用:
            经 :meth:`_emit_status` 为每条被收口的调用发出一次 ``cancelled`` 终态事件；事件构造或
            入队失败直接向上抛出，不在本方法内降级。
        """

        updated = self._copy()

        # 只有 ``create`` 发过创建事件、前端存在 part 的可执行调用需要收口；``blocked_calls`` 从未
        # 投影为 part，收口没有对象（发事件只会让 projector 记一条「part 缺失」告警）。
        for record in list(updated.valid_calls.values()):
            if record is None or record.status not in {"pending", "running"}:
                continue
            if not self._valid_tool_name(record.tool_name) or record.tool_name not in self.allows_tools:
                continue
            updated._emit_status(
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                call_id=record.tool_call_id,
                to_status="cancelled",
            )
            updated.valid_calls[record.tool_call_id].status = "cancelled"
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

        **已知且有意的例外：终态可能早于本方法**。执行层在工具跑完（process 路径为进程强杀与输出
        排空完成）之后，已把终态提前投影进进程内 snapshot（见
        ``tool_terminal_projection.project_tool_terminal_state``），使用户不必等整批工具跑完就能
        看到结果；本方法随后写 ``ToolMessage`` 并再发一次同值终态，由投影按自迁移幂等吸收。该例外
        不产生无法重建的状态：快照是进程内 working copy，进程终止后由冷重建从数据库重建，而重建对
        没有 ``ToolMessage`` 行的 part 默认投影为 ``cancelled``
        （``ConversationTaskStateRebuilder.build_pair_tool_part``），与「结果丢失且 run 已收敛」
        的事实一致；代价是进程在写 ``ToolMessage`` 之前终止时，该 part 会从 ``completed`` /
        ``failed`` 回退为 ``cancelled``。

        参数:
            task_id, run_id, step_id: 事件定位三元组。
            summary: 单条工具观察摘要（``tools`` 节点的 ``dataclasses.asdict`` 投影）。

        返回:
            ``(更新后的 manager, 本条观察推导出的终态)``；调用已处于终态时只返回新快照与该终态，
            不写上下文、不发事件（记录内既有状态保持不变）。

        异常:
            KeyError: 摘要缺少必需字段（见 :func:`_summary_to_observation`）；本方法不兜底。
            RuntimeError / KeyError / TypeError: 仅可在 graph 运行上下文内调用
                （:func:`_runtime_context` / :func:`_runtime_config` / ``get_stream_writer`` 取不到
                上下文时抛出）。
            pydantic.ValidationError: 补建记录时摘要的 ``tool_name`` 为空串（记录字段
                ``min_length=1``），或 :meth:`_emit_status` 的事件字段不满足契约。

        副作用:
            经 ``RuntimeContextManager.add_message`` 写一条 ``ToolMessage``：该调用尚无结果行时
            追加，已有结果行（取消/崩溃遗留的 ``cancelled`` 占位）时按配对规则**原地覆盖**，使真实
            结果不被占位阻塞也不并排留下第二行；两种写入都记 info
            ``tool_observation_persisted``（带 ``canonical_write`` 区分）。覆盖同样会发终态事件，
            前端据此从占位状态翻到真实终态。
            事件构造或入队失败直接向上抛出，不在本方法内降级。记录不存在时即时补建一条 ``pending``
            记录再落终态。命中 ``blocked_calls`` 的记录只写 ``ToolMessage``、不发终态事件（前端无
            part，属隐藏闭合）。
        """

        observation = _summary_to_observation(summary)
        event_status = _event_status(observation.status)
        call_id = summary["tool_call_id"]
        existing = self.valid_calls.get(call_id)
        if existing is None:
            existing = self.blocked_calls.get(call_id)
        if existing is not None and existing.status in {"completed", "failed", "cancelled"}:
            return self._copy(), event_status
        updated = self._copy()
        # ``blocked_calls`` 的调用同样会送达执行层（由 ``ToolAccessGate`` 拒绝），因此也要在此结算；
        # 记录保持在原集合内演化，不跨集合搬移。
        blocked = call_id in updated.blocked_calls
        record = updated.blocked_calls.get(call_id) if blocked else updated.valid_calls.get(call_id)
        if record is None:
            # 正常路径一定先 create；保留记录可让恢复后的 state 反映实际终态。
            presentation = self._presentation_for(summary["tool_name"])
            record = ToolCallLifecycleRecord(
                tool_call_id=call_id,
                tool_name=summary["tool_name"],
                status="pending",
                presentation=presentation,
            )
            updated.valid_calls[call_id] = record
        runtime_context: RuntimeContextManager = _runtime_context()
        operations: WorkflowOperations = _runtime_config().operations
        record.status = event_status
        result_display_data = _ui_data(summary)
        status_hint = _ui_error(summary, event_status)
        transport_metadata = TransportMetadata(
            status=event_status,
            display_data=result_display_data,
            error=status_hint,
        )
        # ``add_message`` 命中同一条调用的既有结果行（取消/崩溃遗留的 cancelled 占位）时会原地
        # 覆盖它，因此真实结果既不会被拒，也不会与占位并排留下第二行。
        canonical_write = runtime_context.add_message(
            operations.to_tool_model_message(observation),
            transport_metadata=transport_metadata,
        )
        log.info(
            "tool_observation_persisted",
            extra={
                "msg": "canonical tool observation 已持久化",
                "data": {
                    "task_id": task_id,
                    "run_id": run_id,
                    "tool_call_id": call_id,
                    "status": event_status,
                    "canonical_write": canonical_write,
                },
            },
        )
        # 刻意先落库上下文、再发终态事件：否则 projector 可能发布一个无法从 context
        # 重建的终态工具状态。
        if blocked:
            # 隐藏闭合：blocked 调用从未发创建事件、前端无 part，发终态事件只会让 projector 记一条
            # 「part 缺失」告警；模型侧协议已由上面的 ToolMessage 闭合。
            return updated, event_status
        updated._emit_status(
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

        连续失败计数规则：``completed`` 清零；``failed`` 且 ``retryable is False`` 时累加（同时
        累加本批 ``error_count``）；``cancelled`` 与可重试失败既不计也不清零。
        ``inherited_error_count`` 是上一批留下的计数。

        参数:
            task_id, run_id, step_id: 事件定位三元组。
            summaries: 本批观察摘要列表。
            inherited_error_count: 继承自 state 的连续失败计数。

        返回:
            ``SettlementResult``：更新后的连续失败计数（``tool_error_count``）、本批「不可重试
            失败」条数（``error_count``）与 lifecycle 快照。

        异常:
            KeyError / RuntimeError / TypeError / pydantic.ValidationError: 同 :meth:`settle`
                （本方法不兜底，异常向上冒泡）。

        副作用:
            逐条经 :meth:`settle` 写模型上下文并发终态事件（``blocked_calls`` 的观察同样在此结算，
            但只写 ``ToolMessage``、不发终态事件）；结束时记一条 ``observe_node_dispatch_completed``
            汇总日志。
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
            elif event_status == "failed" and summary["retryable"] is False:
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
