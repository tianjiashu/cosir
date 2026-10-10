"""ReAct-like 工作流的工具节点（``_tools_node``）。

本模块只承载「工具执行」单一职责：执行本批尚未起跑的调用，并把本批结果事实登记进 graph state。
本节点只读取工具定义中的 ``need_HIL`` 来拒绝混有 HIL 工具的多调用批次，不处理用户决定；实际工具
返回的 ``ToolObservation.user_input_request`` 由 ``wait_user`` 负责派生、投影、挂起与消费。

执行集合取 ``valid_calls`` + ``blocked_calls`` 中**尚未起跑**的记录（``status == "pending"``）；
合法批次由 ``begin`` 把它们迁移为 ``running`` 并随 patch 写回 state；违规 HIL 批次保持 ``pending``，
由 ``observe`` 直接结算为失败，使前端不会看到未执行调用短暂进入 ``running``。
「用户批准后重执行」由 ``wait_user`` 负责（``reopen_for_approved_replay`` 把被批准的记录重置回
``pending`` 并附上用户决定），因此本节点**不需要**知道调用为何待执行——首次执行与批准后重执行
在上面这条判据下完全同构，也就没有「同批其它调用被二次执行」的风险。

本节点登记的可序列化事实：
- ``last_tool_results``：本批 ``ToolObservation`` 的 ``dataclasses.asdict`` 投影（键名与执行层
  字段一致，含 ``tool_call_id`` / ``display_data``），按 ``tool_call_id`` 合并进本模型步的批次；
- ``terminal_sessions``：终端会话展示元数据 allowlist 投影；
- ``tool_call_lifecycle``：``begin`` 迁移后的快照。

不做的：终态事件分发（``completed`` / ``failed`` / ``cancelled``）、模型上下文写回、错误计数与
上限判定（收敛到 ``observe``）；待决请求的派生、投影、挂起与决定消费（收敛到 ``wait_user`` 与
``user_input_projection``）。

**剩余缺口（未决）**：LangGraph 的 checkpoint 以节点为单位——本节点执行期间（工具正在跑）进程
退出时，checkpoint 仍停留在进入本节点之前（记录仍是 ``pending``），因此续跑重入本节点仍会重放
本批调用，对 ``write_file`` / ``apply_patch`` / ``delete_file`` / ``execute_terminal`` 这类有副作用
工具会造成二次执行。彻底消除必须在「工具起跑前」落盘一份「已起跑」事实（按 run_id + step_id +
tool_call_id 的幂等键）作为过滤依据，该事实源尚未建立。模型协议的配对闭合由
``model_node.load_message`` 兜底。与模型节点共享的运行时原语见 ``common``。
"""

import asyncio
import dataclasses
from typing import Any

from langchain_core.messages import SystemMessage

from app.config.logging.logger import log
from app.core.tools.schemas import ToolCall
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.workflows.react.node_helper.common import _runtime_config
from app.core.workflows.react.node_helper.tool_call_lifecycle import ToolCallLifecycleRecord
from app.core.workflows.react.worflow_state.route import ReactRoute
from app.core.workflows.react.worflow_state.state import ReactGraphState

_TERMINAL_CHECKPOINT_FIELDS = frozenset(
    {
        "session_id",
        "status",
        "initial_cwd",
        "shell_kind",
        "first_available_seq",
        "next_seq",
        "exit_code",
        "end_reason",
    }
)


def _project_terminal_sessions(
    previous: dict[str, dict[str, Any]], observations: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """把终端工具展示元数据合并到当前 Run 的 checkpoint state。

    只接受 ``display_data.kind == terminal-session`` 的 allowlisted 字段；工具结果中的
    原始 output、诊断文本和进程对象不会进入 checkpoint。活终端清理由 runtime registry
    负责，不依赖这个投影执行。
    """

    projected = {session_id: dict(value) for session_id, value in previous.items()}
    for observation in observations:
        display_data = observation.get("display_data")
        if not isinstance(display_data, dict) or display_data.get("kind") != "terminal-session":
            continue
        session_id = display_data.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        current = projected.setdefault(session_id, {"session_id": session_id})
        for field in _TERMINAL_CHECKPOINT_FIELDS:
            value = display_data.get(field)
            if value is not None:
                current[field] = value
    return projected


def _merge_batch_results(
    previous: dict[str, Any],
    incoming: list[dict[str, Any]],
    *,
    instruction: str,
) -> dict[str, Any]:
    """把本批观察并入既有批次，按 ``tool_call_id`` 覆盖同一条调用的观察。

    为什么必须合并而不是整批覆盖：同一模型步的调用可能分多遍执行（用户批准后重跑被批准的调用），
    整批覆盖会丢掉同批其它调用的结果，``observe`` 结算时就会少写 ``ToolMessage``（模型侧的配对
    出现空洞）。批次以 ``observe`` 的结算为界（结算后清空 ``last_tool_results``），因此首遍合并的
    起点是空批次，与「覆盖」等价。

    ``instruction`` 由调用方显式给出，不从 ``previous`` 读取：批次起点为空时 ``previous`` 里没有
    instruction，从它读取会把模型本步的伴随文本丢掉。

    参数:
        previous: 既有 ``last_tool_results``（``instruction`` / ``observations``）。
        incoming: 本遍执行产出的观察摘要列表。
        instruction: 本模型步的 instruction（``state.instruction``），每遍执行都传同一个值。

    返回:
        合并后的新 dict：``instruction`` 为入参值，``observations`` 按入参顺序用新观察覆盖同
        ``tool_call_id`` 的旧项，新调用追加在尾部。

    异常:
        无（缺失 ``tool_call_id`` 的观察原样追加，交由下游契约校验）。

    副作用:
        无（纯函数，不修改入参）。
    """

    existing = previous.get("observations")
    merged: list[dict[str, Any]] = list(existing) if isinstance(existing, list) else []
    index = {
        str(item.get("tool_call_id") or ""): position
        for position, item in enumerate(merged)
        if isinstance(item, dict) and item.get("tool_call_id")
    }
    for observation in incoming:
        call_id = str(observation.get("tool_call_id") or "")
        position = index.get(call_id)
        if position is None:
            index[call_id] = len(merged)
            merged.append(observation)
            continue
        merged[position] = observation
    return {
        "instruction": instruction,
        "observations": merged,
    }


def _to_tool_call(record: ToolCallLifecycleRecord) -> ToolCall:
    """把一条生命周期记录还原为 ``ToolCall``（本模块唯一的记录→调用映射点）。

    用户决定取自记录的 ``user_decision`` 字段，而不是 ``arguments``：参数面向模型可见可写，把
    「已获批准」放进参数等于把审批绕过口开在模型协议层。决定由 ``wait_user`` 在重开执行门控时
    写入记录，本节点只做搬运。

    参数:
        record: ``ToolCallLifecycleRecord`` 实例。

    返回:
        等价的 ``ToolCall``；记录字段缺失时以空值构造（由 ``ToolCall`` 自身校验）。

    异常:
        无。

    副作用:
        无。
    """

    return ToolCall.from_dict(
            {
                # 直接取属性：record 是本仓的 ``ToolCallLifecycleRecord``，字段改名必须让
                # AttributeError 暴露，不能靠 getattr 默认值静默吞成空工具名 / 空参数。
                "tool_name": record.tool_name,
                "call_id": record.tool_call_id,
                "arguments": record.args,
                "user_decision": record.user_decision,
            }
        )


async def _tools_node(state: ReactGraphState) -> dict:
    """ReAct 工具节点：执行本批尚未起跑的调用，并登记本批结果事实。

    本节点按工具定义检查 HIL 批次约束，再执行合法调用并登记结果；包含 HIL 工具的多调用批次整体
    生成可修正的失败观察，延迟提示下一次模型调用，所有结果仍由 ``observe`` 结算。待决请求的派生与
    消费下沉到 ``wait_user``。

    本节点为 ``async``，工具批次执行经 ``asyncio.to_thread`` 移出事件循环线程：
    ``execute_terminal`` 会同步阻塞至命令结束（最长 ``max_command_timeout``），
    若在事件循环线程内直跑，会连带卡死 SSE 推送与全部并发请求。生命周期回调在协程内
    构造后闭包捕获，并随工具执行传入工作线程，不依赖 LangGraph stream writer。

    参数:
        state: 当前 graph state，经 ``tool_call_lifecycle`` 携带本批调用记录与 instruction。

    返回:
        需要合并回 graph state 的增量：``last_tool_results``（本批观察摘要，按 ``tool_call_id``
        合并进本模型步批次，可落 checkpoint）、``terminal_sessions``（终端会话展示元数据投影）与
        ``tool_call_lifecycle``（执行批次经 ``begin`` 迁移后的快照；拦截批次保留原快照）、
        ``next_node``（待决请求进入
        ``wait_user``，其它观察进入 ``observe``）。

        执行集合按 ``pending`` 判定：合法批次的记录在送执行层前迁移为 ``running``，重入本节点
        （自环 / 重放）不会对同一批调用二次发起；被拦截批次不迁移，直接由 ``observe`` 结算。

    异常:
        RuntimeError: ``state.tool_call_lifecycle`` 缺失（应由 ``model`` 节点写入），或 ``begin``
            发状态事件时取不到 graph 运行上下文（``get_stream_writer``）。
        KeyError / TypeError: 同 ``begin``（运行上下文缺失导致取不到 stream writer）。
        pydantic.ValidationError: ``begin`` 发出的状态事件字段不满足契约，不在本节点内降级。

    副作用:
        - 执行工具（文件、终端、搜索、委派等）并产出工具生命周期事实；状态写入 **run**；
        - ``running`` 状态事件由 ``begin`` 在本节点发出；终态事件不在本节点发出，也不在此
          收口取消（两者分别由执行层出口投影与 ``observe`` 节点的 ``settle`` 负责）；
        - 多调用批次包含 HIL 工具时不运行 handler，写入可重试观察并向当前 Task 的延迟系统消息队列
          投递模型修正指令；
        - 模型协议层面的配对闭合统一由 ``RuntimeContextManager.load_message`` 在下次取数时
          自动补 ``ToolMessage`` 占位，本节点不构造/落库占位消息、亦不越界访问 service
          受保护成员。
    """
    lifecycle = state.tool_call_lifecycle
    if lifecycle is None:
        # 先校验 state 契约、再取运行期依赖：这样「缺 lifecycle」必然以 docstring 承诺的错误暴露，
        # 不会被无关的运行时取数异常掩盖。
        raise RuntimeError("tool_call_lifecycle is required before tools_node execution")

    rc = _runtime_config()  # 取运行时配置
    operations = rc.operations  # 领域操作
    task = operations.get_current_task()  # 任务（工具执行需要 task_id）
    task_id = task.id
    step_id = f"step-{state.step_count}"  # 复用上一步 step_id（工具是 model 步的延续）
    instruction = state.instruction

    # 执行集合 = 尚未起跑的记录。首次执行时它们是 ``model`` 节点 ``classify`` 建好的 ``pending``；
    # 批准后重执行时由 ``wait_user`` 经 ``reopen_for_approved_replay`` 重置回 ``pending`` 并附上
    # 用户决定，两种来源在这条判据下同构——本节点不区分「首次」与「重执行」。「未起跑」等价于
    # ``pending`` 的前提是 ``begin`` 覆盖了全部送执行层的记录（含禁用工具与未注册工具名这类隐藏
    # 调用），见 ``ToolCallLifecycleManager.begin``。
    pending_records = [
        record
        for record in lifecycle.valid_tools + lifecycle.blocked_tool_calls
        if record.status == "pending"
    ]
    calls = [_to_tool_call(record) for record in pending_records]

    definitions_by_name = {definition.name: definition for definition in operations.all_vaild_tools}
    hil_tool_names = sorted(
        {
            call.tool_name
            for call in calls
            if (definition := definitions_by_name.get(call.tool_name)) is not None
            and definition.need_HIL
        }
    )
    blocked_batch = len(calls) > 1 and bool(hil_tool_names)

    log.info(
        "tools_node_started",
        extra={
            "msg": f"准备执行 {len(calls)} 个工具调用，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "call_count": len(calls),
                "call_ids": [call.call_id for call in calls],
                "instruction": instruction,
            },
        },
    )

    if blocked_batch:
        # 整批拒绝，避免并行调用中的普通工具先产生副作用、HIL 工具再等待用户确认。
        observations = [
            tool_error(
                tool_name=call.tool_name,
                error="error",
                reason="blocked_batch",
                retryable=True,
                tool_call_id=call.call_id,
            )
            for call in calls
        ]
        from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

        task_runtime_spaces.get_or_create(task_id).defer_system_message(
            SystemMessage(
                content=(
                    f"The tool(s) {', '.join(hil_tool_names)} require human approval and can "
                    "only be called alone in a tool batch. The previous batch was blocked and "
                    "none of its tools ran. Retry the human approval tool in a separate batch."
                ),
                additional_kwargs={"run_id": rc.run.id},
            )
        )
        next_node = ReactRoute.OBSERVE
        log.warning(
            "tools_node_hil_batch_blocked",
            extra={
                "msg": "包含人工确认工具的多调用批次已整体拦截，未执行任何工具",
                "data": {
                    "run_id": rc.run.id,
                    "step_id": step_id,
                    "hil_tool_names": hil_tool_names,
                    "call_ids": [call.call_id for call in calls],
                },
            },
        )
    else:
        lifecycle = lifecycle.begin(task_id=task_id, run_id=rc.run.id, step_id=step_id)
        tool_run = await operations.run_tool_calls(
            task_id, calls, step_id, asyncio.get_running_loop()
        )
        observations = tool_run.observations
        next_node = (
            ReactRoute.WAIT_USER
            if any(observation.user_input_request is not None for observation in observations)
            else ReactRoute.OBSERVE
        )

    log.info(
        "tools_node_tool_run",
        extra={
            "msg": f"工具批次执行结果，step_id={step_id}",
            "data": {
                "blocked_batch": blocked_batch,
                "observations": [dataclasses.asdict(observation) for observation in observations],
            },
        },
    )

    # 终态事件（completed/failed/cancelled）、模型上下文写回与错误计数统一收敛到
    # observe 节点（经 ToolCallLifecycleManager.settle_batch 分发），本节点只产出治理摘要。
    log.info(
        "tools_node_completed",
        extra={
            "msg": f"工具批次处理完成，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "tool_count": len(observations),
            },
        },
    )

    observation_dicts = [dataclasses.asdict(observation) for observation in observations]

    return {
        # 恒定按 ``tool_call_id`` 合并：同一模型步可能分多遍执行（批准后重跑被批准的调用），
        # 整批覆盖会丢掉同批其它调用的结果，observe 结算时就会少写 ToolMessage（模型侧配对
        # 出现空洞）。批次边界由 observe 的结算（清空 last_tool_results）划定，故首遍合并的
        # 起点是空批次，与「覆盖」等价。
        "last_tool_results": _merge_batch_results(
            state.last_tool_results,
            observation_dicts,
            instruction=instruction or "",
        ),
        "terminal_sessions": _project_terminal_sessions(
            state.terminal_sessions,
            observation_dicts,
        ),
        "tool_call_lifecycle": lifecycle,
        "next_node": next_node,
    }
