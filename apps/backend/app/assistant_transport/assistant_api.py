"""Assistant UI Assistant Transport API。

本模块是新的对话传输接入层：接收 assistant-ui ``add-message`` 命令，创建运行切片，
并把服务端 canonical conversation facts 编码为 Assistant Transport state operations。
它不把 assistant-ui 类型传入 core、service 或 storage。
"""

from assistant_stream.serialization import AssistantTransportResponse
from fastapi import Depends, HTTPException, Response
from fastapi.responses import JSONResponse

from app.app import app
from app.assistant_transport.request import (
    AddMessageCommand,
    AssistantAttachRequest,
    AssistantTransportRequest,
)
from app.assistant_transport.service.conversation_run_executor import ConversationRunExecutor
from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.assistant_transport.service.transport_assistant_service import (
    TransportAssistantService,
    _raise_transport_error,
)
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.config.logging.logger import log
from app.core.runtime.runner import AgentRuntime
from app.service.attachment.image_normalizer import ImageNormalizationError
from app.service.depends import (
    get_conversation_run_executor,
    get_conversation_task_state_service,
    get_runtime,
    get_task_service,
    get_transport_assistant_service,
)
from app.task_runtime.service.task_service import TaskService


@app.post("/assistant")
async def assistant_transport(
    request: AssistantTransportRequest,
    run_executor: ConversationRunExecutor = Depends(get_conversation_run_executor),
    runtime: AgentRuntime = Depends(get_runtime),
    transport_service: TransportAssistantService = Depends(get_transport_assistant_service),
) -> AssistantTransportResponse:
    """接收用户消息并返回 Assistant Transport 状态流。

    参数:
        request: Assistant UI request 请求，当前业务命令为文本 ``add-message``。
        state_service: 负责读取 Task Transport state 的唯一 owner。
        run_service: 在一个事务中占用 command 并创建、绑定 Conversation Run 的 service。
        run_executor: 进程级后台执行器，负责驱动 AgentRuntime 执行。

    返回:
        使用 ``assistant-stream`` 编码的 ``text/event-stream`` 响应。

    异常:
        HTTPException: 请求任务不存在、命令冲突或运行切片创建失败时抛出。

    副作用:
        创建一个 pending run，并立即启动后台 Agent 执行（与 HTTP 订阅解耦）；
        运行期间更新数据库和 canonical conversation facts。
    """

    command = next(
        (command for command in request.commands if isinstance(command, AddMessageCommand)),
        None,
    )
    # 确定 run 模式：resume / edit / new
    mode = transport_service.classify_run_command(command, request.runId)

    # 确保 run 目标存在
    task_id = transport_service.ensure_run_target(
        request=request,
        command=command,
        mode=mode,
    )


    try:
        with transport_service.task_run_operation(task_id=task_id):
            # 准备 run 启动结果
            start_result = await transport_service.prepare_run_start(
                task_id=task_id,
                command=command,
                mode=mode,
                request=request,
            )
            run = start_result.run
            initial_state = start_result.initial_state

            if not start_result.created:
                _raise_transport_error(
                    409,
                    "RUN_START_CONFLICT",
                    "run already exists",
                    retryable=True,
                    command_id=command.commandId if command is not None else None,
                    run_id=request.runId,
                )

            try:
                await run_executor.start(
                    run.id,
                    lambda execution_run: runtime.execute_run(
                        execution_run,
                        execution_mode=start_result.execution_mode,
                    ),
                )
            except Exception as exc:
                # 真失败：run 已被本次请求置为 active，但执行器没有起来，必须收敛，否则该 task
                # 会残留一个无执行器的 active run（new 与 resume 都会被状态校验拒绝）。
                transport_service.settle_run_start_failure(run.id)
                log.exception(
                    "assistant_transport_executor_start_failed",
                    extra={
                        "msg": "执行器启动失败，已尝试收敛该 run",
                        "data": {"run_id": run.id, "task_id": task_id},
                    },
                )
                _raise_transport_error(
                    500,
                    "RUN_START_FAILED",
                    str(exc),
                    retryable=True,
                    command_id=command.commandId if command is not None else None,
                    run_id=request.runId,
                )

            return transport_service.build_response(
                task_id=task_id,
                thread_id=f"task-{task_id}",
                run_id=run.id,
                state=initial_state,
            )
    except HTTPException:
        # Domain conflict responses raised by ``_raise_transport_error`` must
        # reach FastAPI unchanged.  Converting them to RUN_START_FAILED would
        # hide actionable states such as RUN_NOT_RESUMABLE and TASK_BUSY.
        raise
    except ImageNormalizationError as exc:
        _raise_transport_error(
            503 if exc.code == "ATTACHMENT_STORAGE_UNAVAILABLE" else 400,
            exc.code,
            exc.message,
            retryable=exc.code in {"IMAGE_CONVERSION_FAILED", "ATTACHMENT_STORAGE_UNAVAILABLE"},
            command_id=command.commandId if command is not None else None,
            run_id=request.runId,
        )
    except ValueError as exc:
        operation_code = (
            "RUN_NOT_RESUMABLE"
            if mode == "resume"
            else "RUN_NOT_REPLAYABLE"
            if mode == "edit"
            else "RUN_START_CONFLICT"
        )
        _raise_transport_error(
            409,
            operation_code,
            str(exc),
            retryable=True,
            command_id=command.commandId if command is not None else None,
            run_id=request.runId,
        )
    except TimeoutError:
        # Task 操作闸门 10s 内未释放：该 task 上有另一个执行正持有运行期闸门。这是
        # "忙"，不是服务故障，映射为可重试的 409 而不是 500。
        _raise_transport_error(
            409,
            "TASK_BUSY",
            "该对话正在执行其它操作，请稍后重试",
            retryable=True,
            command_id=command.commandId if command is not None else None,
            run_id=request.runId,
        )
    except Exception:
        log.exception(
            "assistant_transport_run_start_failed",
            extra={
                "msg": "Assistant Transport 命令与运行切片原子创建失败",
                "data": {
                    "task_id": task_id,
                    "command_id": command.commandId if command is not None else None,
                    "run_id": request.runId,
                },
            },
        )
        _raise_transport_error(
            500,
            "RUN_START_FAILED",
            "无法创建对话运行，请稍后重试",
            retryable=True,
        )


@app.post("/tasks/{task_id}/assistant/attach")
async def assistant_transport_attach(
    task_id: int,
    request: AssistantAttachRequest,
    task_service: TaskService = Depends(get_task_service),
    transport_service: TransportAssistantService = Depends(get_transport_assistant_service),
) -> AssistantTransportResponse:
    """重新订阅已有 Run，不触发业务 resume。"""

    try:
        task_service.get_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    if request.threadId != f"task-{task_id}":
        _raise_transport_error(
            409,
            "THREAD_TASK_MISMATCH",
            "threadId 与 task_id 不一致",
            retryable=False,
            run_id=request.runId,
        )
    if request.taskId is not None and request.taskId != task_id:
        _raise_transport_error(
            409,
            "TASK_ID_MISMATCH",
            "请求 taskId 与路径不一致",
            retryable=False,
            run_id=request.runId,
        )
    if request.commands:
        _raise_transport_error(
            400,
            "ATTACH_COMMANDS_UNSUPPORTED",
            "attach 请求不能携带业务命令",
            retryable=False,
            run_id=request.runId,
        )
    return await transport_service.attach_run(
        task_id=task_id,
        thread_id=request.threadId,
        run_id=request.runId,
    )


@app.get("/tasks/{task_id}/assistant/state")
async def assistant_transport_state(
    task_id: int,
    task_service: TaskService = Depends(get_task_service),
    state_service: ConversationTaskStateService = Depends(
        get_conversation_task_state_service
    ),
) -> ConversationStateSnapshot:
    """返回某任务的首屏历史 state（服务端权威对话视图）。

    参数:
        task_id: URL 中的任务标识。
        task_service: 用于校验任务存在性的领域 service（不直接触碰 storage）。
        state_service: Task Transport state 的读取边界。

    返回:
        与 Assistant Transport state 形状一致的中性 wire state 字典；任务无 Run 时
        ``runs`` 为空数组。

    异常:
        HTTPException: 任务不存在时返回 404（复用 ``task_service.get_task`` 的
        ``KeyError`` 约定，与 ``assistant_transport`` 创建路径一致）。

    副作用:
        不修改 Run、Context 或执行器；state service 在冷读时从 canonical Task、Run 与
        Context 记录重建 state，并校正进程内 working copy 的生命周期事实。
    """
    # 先校验任务存在：不存在时 ``get_task`` 抛 KeyError → 映射为 404。
    # 不能在投影阶段再判，因为空 task 与不存在 task 在投影层都表现为空 runs。
    try:
        task_service.get_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    state = state_service.get_state(task_id)
    log.info(
        "assistant_snapshot_read",
        extra={
            "msg": "读取 Assistant 历史快照",
            "data": {
                "task_id": task_id,
                "run_id": state["current_run_id"],
                "run_status": next(
                    (
                        run["status"]
                        for run in state["runs"]
                        if run["runId"] == state["current_run_id"]
                    ),
                    "idle",
                ),
                "message_count": sum(len(run["messages"]) for run in state["runs"]),
            },
        },
    )
    return state


@app.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: int,
    run_executor: ConversationRunExecutor = Depends(get_conversation_run_executor),
) -> Response:
    """显式取消一个 Conversation Run。

    表现层只做输入校验、调用业务层与异常映射，不编排取消时序——「向进程内取消信号源
    标记该 run」已收口到 ``ConversationRunExecutor.cancel`` 单一入口。HTTP 断连不会
    调用本端点；重复取消只返回既有信号状态，不重复标记，也不改变任何持久化事实。

    本端点只表示**取消信号已被接受**，不表示 run 已经终结：run 的终态转移
    （active → cancelled）由 workflow 经 run_service 落定，前端需继续以 canonical
    snapshot 为准判断该 run 是否已收束。

    参数:
        run_id: Conversation Run 标识。
        run_executor: 进程内取消信号标记的唯一入口。

    返回:
        包含 ``run_id`` 与 ``cancelled`` 的 JSON 对象。

    异常:
        HTTPException: run 不存在时返回 404；该 run 此前已标记过取消（信号已存在）时
        返回 409；标记内部失败时返回 500。

    副作用:
        向进程内取消信号源写入该 run；不落库 run 状态、不取消后台执行 task。
    """
    try:
        result = await run_executor.cancel(run_id, end_reason="user_cancelled")
    except KeyError as exc:
        log.warning(
            "conversation_run_cancel_not_found",
            extra={"msg": "取消请求未找到 Conversation Run", "data": {"run_id": run_id}},
        )
        raise HTTPException(status_code=404, detail="run not found") from exc
    except Exception as exc:
        log.exception(
            "cancel_run_failed",
            extra={"msg": "取消 Conversation Run 失败", "data": {"run_id": run_id}},
        )
        raise HTTPException(status_code=500, detail="failed to cancel run") from exc
    if not result:
        log.info(
            "conversation_run_cancel_rejected",
            extra={"msg": "Conversation Run 当前状态不允许取消", "data": {"run_id": run_id}},
        )
        raise HTTPException(status_code=409, detail="run is not in a cancellable state")
    log.info(
        "conversation_run_cancelled",
        extra={
            "msg": "Conversation Run 已取消",
            "data": {"run_id": run_id, "result": result},
        },
    )
    return JSONResponse(
        content={
            "run_id": run_id,
            "cancelled": result,
        }
    )


@app.post("/runs/{run_id}/tool-calls/{tool_call_id}/cancel")
async def cancel_tool_call(
    run_id: int,
    tool_call_id: str,
    run_executor: ConversationRunExecutor = Depends(get_conversation_run_executor),
) -> Response:
    """显式取消一个正在执行的工具调用（工具级取消）。

    表现层只做输入校验、调用业务层与异常映射，不编排取消时序——「向进程内工具级取消
    信号源标记该工具调用」已收口到 ``ConversationRunExecutor.cancel_tool_call`` 单一入口。

    与 ``POST /runs/{run_id}/cancel`` 的差别是取消范围：run 级取消要求整个 Conversation
    Run 停止执行，run 终态由 workflow 落定；本端点只中止**一次**工具调用，run 与 Agent
    在其后继续执行。因此本端点只表示**取消信号已被接受**，不表示工具调用已经结束：被
    点名的工具在执行层检出信号后返回取消观察，其终态与模型侧 ``ToolMessage`` 由 workflow
    落定，前端需继续以 canonical snapshot 为准。HTTP 断连不会调用本端点。

    响应码的语义边界：信号随该工具调用执行结束被释放，run 收尾时按 run_id 兜底清理，
    因此 409 只表示「此刻信号已存在」，200 也不保证该调用尚未结束——**调用方不得把
    200/409 当作幂等去重或执行进度依据**，只能以 404（run 不存在）作为确定性失败。

    参数:
        run_id: 目标工具调用所属的 Conversation Run 标识。
        tool_call_id: 目标工具调用标识（模型工具调用 id）。
        run_executor: 进程内取消信号标记的唯一入口。

    返回:
        包含 ``run_id``、``tool_call_id`` 与 ``cancelled`` 的 JSON 对象。

    异常:
        HTTPException: run 不存在时返回 404；该工具调用此前已标记过取消（信号已存在）时
        返回 409；标记内部失败时返回 500。

    副作用:
        向进程内工具级取消信号源写入 ``(run_id, tool_call_id)``；不落库 run 或工具调用
        状态、不取消后台执行 task。工具执行层在本次调用结束时释放该信号，run 收尾时按
        run_id 兜底清理。
    """
    try:
        result = await run_executor.cancel_tool_call(run_id, tool_call_id)
    except KeyError as exc:
        log.warning(
            "tool_call_cancel_run_not_found",
            extra={
                "msg": "工具级取消请求未找到 Conversation Run",
                "data": {"run_id": run_id, "tool_call_id": tool_call_id},
            },
        )
        raise HTTPException(status_code=404, detail="run not found") from exc
    except Exception as exc:
        log.exception(
            "tool_call_cancel_failed",
            extra={
                "msg": "取消工具调用失败",
                "data": {"run_id": run_id, "tool_call_id": tool_call_id},
            },
        )
        raise HTTPException(status_code=500, detail="failed to cancel tool call") from exc
    if not result:
        log.info(
            "tool_call_cancel_rejected",
            extra={
                "msg": "工具调用已存在取消信号，重复取消被拒绝",
                "data": {"run_id": run_id, "tool_call_id": tool_call_id},
            },
        )
        raise HTTPException(status_code=409, detail="tool call is already cancelled")
    log.info(
        "tool_call_cancelled",
        extra={
            "msg": "工具调用取消信号已接受",
            "data": {"run_id": run_id, "tool_call_id": tool_call_id},
        },
    )
    return JSONResponse(
        content={
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "cancelled": result,
        }
    )
