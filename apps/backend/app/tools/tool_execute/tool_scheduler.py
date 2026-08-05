"""工具调度器：对模型请求的工具调用做权限门禁 + 参数校验 + 隔离执行编排。"""

import dataclasses
import enum
import json
from collections.abc import Callable, Collection

from app.config.logging.logger import log
from app.models.file_snapshot_record import FileSnapshotRecord
from app.storage.crud.file_snapshot_crud import FileSnapshotCrud
from app.tools.guard.display_data_budget import DisplayDataBudget
from app.tools.guard.file_resource_paths import FileResourcePathError
from app.tools.guard.file_tool_state_coordinator import (
    FileToolStateCoordinator,
)
from app.tools.guard.tool_output_budget import ToolOutputBudget
from app.tools.schemas import (
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.tools.tool_execute.tool_error import tool_error
from app.tools.tool_execute.tool_executor import ToolExecutor
from app.tools.tool_handler.patch.patch_parser import PatchOperation
from app.tools.tool_handler.patch.v4a_reverse import (
    build_forward_operations,
    reverse_v4a_operation,
)
from app.tools.tool_registry import ToolRegistry
from app.tools.validation.arguments import validate_tool_arguments

_WORKSPACE_REQUIRED_PERMISSIONS = frozenset({"file_write", "file_delete"})


class ToolScheduler:
    """模型请求工具调用的统一调度入口。

    单一职责：对模型请求的工具调用做「权限策略校验 + 参数校验 + 隔离执行」
    的三段式编排，并始终返回归一化的 :class:`ToolObservation`，使上层
    （workflow / 运行时）无需关心失败原因细节。

    职责边界：
    - 负责：从注册表解析工具定义、按 ``allowed_permissions`` 做权限门禁、
      调用 ``validation`` 做参数校验、委派 :class:`ToolExecutor` 隔离执行。
    - 不负责：子进程隔离与超时强杀（``ToolExecutor``）、handler 业务逻辑、
      跨进程日志桥接、模型可见性之外的运行策略。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        state_coordinator: FileToolStateCoordinator | None = None,
        output_budget: ToolOutputBudget | None = None,
        display_data_budget: DisplayDataBudget | None = None,
    ) -> None:
        """初始化调度器并固化权限策略。

        参数:
            registry: 工具注册表，提供工具定义查询。
            state_coordinator: 文件 revision、重复调用和路径锁协作者。
            output_budget: 模型可见 ``content`` 的统一输出预算。
            display_data_budget: 客户端展示数据通道的统一字符预算。

        返回:
            无。

        异常:
            无。

        副作用:
            持有 ``registry``、``state_coordinator`` 和
            ``executor`` 引用；不触发任何工具执行。
        """

        self._registry = registry
        self._executor = ToolExecutor()
        self._state_coordinator = state_coordinator or FileToolStateCoordinator()
        self._output_budget = output_budget or ToolOutputBudget()
        self._display_data_budget = display_data_budget or DisplayDataBudget()

    def list_tools(self) -> list[ToolDefinition]:
        return self._registry.get_all_definitions()

    def get_tool_definition(self, tool_name: str) -> ToolDefinition | None:
        """按名称查询已注册的工具定义。

        参数:
            tool_name: 工具名称。

        返回:
            命中的 :class:`ToolDefinition`；未注册时返回 None。

        异常:
            无。

        副作用:
            无（只读注册表）。
        """

        return self._registry.get_tool_definition(tool_name)

    def execute(
        self,
        call: ToolCall,
        execution_context: ToolExecutionContext | None = None,
        allowed_tool_names: Collection[str] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> ToolObservation:
        """执行单次工具调用并返回归一化观察结果。

        编排顺序：注册表命中 → 权限门禁 → 参数校验 → 文件工具经状态协调
        （revision/stale/锁）后委派执行器隔离执行，非文件工具直接委派执行器
        隔离执行；
        任一前置环节失败都直接返回带 ``reason`` 的 :class:`ToolObservation`，
        绝不抛出，使上层始终拿到可落库/可回传的结果。

        参数:
            call: 模型请求的工具调用，含工具名、参数与调用 id。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；
                透传给执行器并由 handler 在执行期消费，便于后续扩展更多执行参数。
            allowed_tool_names: 当前 Agent profile 允许运行的工具名；为 None 表示
                调用方不增加 Agent 级门禁。
            should_cancel: 可选取消检查回调；透传给 process 工具执行器用于中止长工具。

        返回:
            归一化后的 :class:`ToolObservation`：成功为 status="success"；
            未知工具 / 权限拒绝 / 参数非法 / 执行失败为 status="error"，
            每种失败均经 :func:`tool_error` 构造，``reason`` 为面向模型的
            富文本（根因 + 修正建议 + 与 ``retryable`` 一致的重试提示），
            而非稳定机器短码。

        异常:
            无（所有失败路径均归一化为 error 观察）。

        副作用:
            委派 :class:`ToolExecutor` 启动子进程执行；可能因权限或参数
            校验失败而短路返回，不进入执行阶段。
        """

        # 检查工具是否存在
        tool = self._registry.get_tool_definition(call.tool_name)
        if tool is None:
            return self._apply_output_budget(
                tool_error(
                    call.tool_name,
                    f"unknown tool: {call.tool_name}",
                    reason=(
                        f"the tool '{call.tool_name}' is not registered in the tool "
                        f"system; this is deterministic, so check the tool name for "
                        f"typos or register the tool before calling it again. The same "
                        f"name will always be rejected."
                    ),
                    tool_call_id=call.call_id,
                ),
                execution_context,
            )

        # 检查工具是否在允许的工具名列表中
        if allowed_tool_names is not None and tool.name not in allowed_tool_names:
            return self._apply_output_budget(
                tool_error(
                    tool.name,
                    f"agent profile denied tool: {tool.name},allow tool names:{allowed_tool_names}",
                    reason=(
                        f"the current agent profile does not allow calling "
                        f"'{tool.name}'; the allowed tools are "
                        f"{sorted(allowed_tool_names)}. This is deterministic under "
                        f"the current profile, so choose an allowed tool or update "
                        f"the profile's allowed_permissions before retrying."
                    ),
                    permission=tool.permission,
                    tool_call_id=call.call_id,
                ),
                execution_context,
            )

        # 检查工具是否需要工作区执行上下文
        if execution_context is None and tool.permission in _WORKSPACE_REQUIRED_PERMISSIONS:
            return self._apply_output_budget(
                tool_error(
                    tool.name,
                    f"{tool.name} requires a workspace execution context",
                    reason=(
                        f"'{tool.name}' requires a workspace execution context "
                        f"(task/workspace/root path), but none was supplied; this is "
                        f"deterministic, so run it inside a task that provides a "
                        f"workspace, otherwise the same call will always fail."
                    ),
                    permission=tool.permission,
                    tool_call_id=call.call_id,
                ),
                execution_context,
            )

        # 检查工具参数是否合法
        validation = validate_tool_arguments(
            call.arguments,
            tool.parameters_schema,
            tool.args_model,
        )

        # 如果参数不合法，则返回错误
        if not validation.ok:
            return self._apply_output_budget(
                tool_error(
                    tool.name,
                    f"invalid tool arguments: {validation.error}",
                    reason=(
                        f"the tool arguments failed validation: {validation.error}; "
                        f"this is deterministic, so fix the argument values/types per "
                        f"the tool's parameter schema and call again. The same "
                        f"arguments will always be rejected."
                    ),
                    permission=tool.permission,
                    tool_call_id=call.call_id,
                ),
                execution_context,
            )
        # 仅触碰文件系统的工具走文件状态协调（revision/stale/锁）；
        # 非文件工具（如 execute_terminal）直接执行，避免把文件协调机制
        # 错配到无关资源上。
        if "filesystem" in tool.resource_keys:
            try:
                plan = self._state_coordinator.prepare(
                    tool,
                    validation.arguments,
                    execution_context,
                    tool_call_id=call.call_id,
                )
            except FileResourcePathError as exc:
                return self._apply_output_budget(
                    tool_error(
                        tool.name,
                        str(exc),
                        reason=str(exc),
                        permission=tool.permission,
                        tool_call_id=call.call_id,
                    ),
                    execution_context,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                return self._apply_output_budget(
                    tool_error(
                        tool.name,
                        f"invalid file path: {exc}",
                        reason=(
                            f"the file path is invalid: {exc}; this is deterministic, "
                            f"so pass a well-formed path inside the project. The same "
                            f"malformed path will always be rejected."
                        ),
                        permission=tool.permission,
                        tool_call_id=call.call_id,
                    ),
                    execution_context,
                )

            if plan.early_observation is not None:
                return self._apply_output_budget(plan.early_observation, execution_context)

            try:
                with self._state_coordinator.lock(plan, execution_context):
                    stale_observation = self._state_coordinator.check_stale(
                        plan,
                        tool,
                        execution_context,
                        tool_call_id=call.call_id,
                    )
                    if stale_observation is not None:
                        return self._apply_output_budget(stale_observation, execution_context)
                    observation = self._executor.execute(
                        tool,
                        validation.arguments,
                        execution_context=execution_context,
                        tool_call_id=call.call_id,
                        should_cancel=should_cancel,
                    )
                    self._state_coordinator.complete(
                        plan,
                        observation,
                        execution_context,
                    )
                    self._record_file_snapshot(tool, observation, execution_context)
            except RuntimeError as exc:
                return self._apply_output_budget(
                    tool_error(
                        tool.name,
                        str(exc),
                        reason=(
                            f"the file state coordinator could not process the "
                            f"request: {exc}; this is usually transient (e.g. a "
                            f"temporary capacity or lock limit), so retrying the same "
                            f"call may succeed once the condition clears."
                        ),
                        retryable=True,
                        permission=tool.permission,
                        tool_call_id=call.call_id,
                    ),
                    execution_context,
                )
        else:
            observation = self._executor.execute(
                tool,
                validation.arguments,
                execution_context=execution_context,
                tool_call_id=call.call_id,
                should_cancel=should_cancel,
            )
            self._record_file_snapshot(tool, observation, execution_context)
        return self._apply_output_budget(observation, execution_context)

    def _record_file_snapshot(
        self,
        tool: ToolDefinition,
        observation: ToolObservation,
        execution_context: ToolExecutionContext | None,
    ) -> None:
        """在文件工具成功执行后采集反向操作快照，供 Turn 回退按 turn 精准还原。

        采集逻辑（方案 §五）：从 ``observation.data["changes"]``（采集层事实
        快照）构造正向 V4A，再反转为反向操作；仅落库「碰过文件的 filesystem 工具」
        且执行成功、且带 ``turn_id`` 的调用。``execute_terminal`` 不产 ``changes``，
        自然跳过（其副作用不入快照，见方案 D5）。采集失败只记 warning 日志，不阻断
        工具主流程（方案 §六 可重入要求）。

        参数:
            tool: 被执行工具的定义（提供 ``name`` 作为快照 ``tool_name``）。
            observation: 归一化后的工具观察结果。
            execution_context: 本次执行的运行时边界（取其 ``turn_id``）。

        返回:
            无。

        异常:
            内部吞掉并转 warning 日志：采集异常绝不冒泡到工具主流程。

        副作用:
            向 ``file_snapshots`` 表写入 0~N 条反向操作记录（每个变更文件一条）。
        """
        if observation.status != "success":
            return
        if execution_context is None or not execution_context.turn_id:
            return
        data = observation.data
        if not data:
            return
        changes = data.get("changes")
        if not changes:
            return
        try:
            forward_ops = build_forward_operations(changes)
            crud = FileSnapshotCrud()
            next_seq = crud.next_seq(execution_context.turn_id)
            for offset, forward in enumerate(forward_ops):
                reverse_op = reverse_v4a_operation(forward)
                crud.save(
                    FileSnapshotRecord(
                        turn_id=execution_context.turn_id,
                        seq=next_seq + offset,
                        tool_name=tool.name,
                        tool_call_id=observation.tool_call_id,
                        path=forward.file_path,
                        action=forward.operation.value,
                        op_json=_reverse_op_to_json(reverse_op),
                    )
                )
        except Exception:
            log.exception(
                "file_snapshot_record_failed",
                extra={
                    "msg": "文件快照采集失败，回退时可能丢失该次文件改动还原能力",
                    "data": {
                        "turn_id": execution_context.turn_id,
                        "tool_name": tool.name,
                    },
                },
            )

    def _apply_output_budget(
        self,
        observation: ToolObservation,
        execution_context: ToolExecutionContext | None,
    ) -> ToolObservation:
        """对任意成功、失败或提前返回观察统一应用输出预算，超出预算截断，并保留本地文件。

        模型通道（``content``）与客户端展示通道（``data``）分别受
        :class:`ToolOutputBudget` 与 :class:`DisplayDataBudget` 约束，避免展示
        通道绕过模型通道预算无约束膨胀。

        参数:
            observation: 待返回给上层的工具观察。
            execution_context: 当前 workspace 上下文。

        返回:
            已脱敏并受两条通道字符预算约束的观察。

        异常:
            无。artifact 写入失败由 :class:`ToolOutputBudget` 内部退化处理。

        副作用:
            超限且有 workspace 时可能写入脱敏 artifact。
        """

        budgeted = self._output_budget.apply(observation, execution_context)
        return self._display_data_budget.apply(budgeted)


def _reverse_op_to_json(reverse_op: "PatchOperation") -> str:
    """把反向 PatchOperation 投影为可 JSON 序列化的字典字符串。

    采用 ``dataclasses.asdict`` 保留与 ``PatchOperation(**data)`` 兼容的嵌套结构
    （``hunks`` → ``{"lines": [{"prefix", "content"}]}``），仅把 ``OperationType``
    枚举落为 ``value`` 字符串以满足 JSON 序列化，确保回退侧可直接
    ``PatchOperation(**json.loads(op_json))`` 重建。

    参数:
        reverse_op: 已构造的反向 PatchOperation（含 OperationType 枚举与嵌套 Hunk）。

    返回:
        JSON 字符串；结构与 ``PatchOperation`` 构造参数一致。

    异常:
        无。

    副作用:
        无。
    """

    def _enum_to_value(obj: object) -> object:
        if isinstance(obj, enum.Enum):
            return obj.value
        if isinstance(obj, dict):
            return {k: _enum_to_value(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_enum_to_value(v) for v in obj]
        return obj

    raw = dataclasses.asdict(reverse_op)
    serializable = _enum_to_value(raw)
    return json.dumps(serializable, ensure_ascii=False)
