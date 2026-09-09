"""Assistant UI Assistant Transport API。

本模块是新的对话传输接入层：接收 assistant-ui ``add-message`` 命令，创建运行切片，
并把服务端 canonical conversation facts 编码为 Assistant Transport state operations。
它不把 assistant-ui 类型传入 core、service 或 storage。
"""

import asyncio

from assistant_stream.serialization import AssistantTransportResponse
from fastapi import Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.app import app
from app.assistant_transport.request import (
    AddMessageCommand,
    AssistantTransportRequest,
)
from app.assistant_transport.service.conversation_run_command_service import (
    ConversationRunCommandService,
)
from app.assistant_transport.service.conversation_run_executor import ConversationRunExecutor
from app.assistant_transport.service.conversation_task_snapshot_service import (
    ConversationTaskSnapshotService,
)
from app.assistant_transport.service.transport_assistant_service import (
    TransportAssistantService,
    _raise_transport_error,
)
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.config.logging.logger import log
from app.core.runtime.runner import AgentRuntime
from app.service.depends import (
    get_conversation_run_command_service,
    get_conversation_run_executor,
    get_conversation_task_snapshot_service,
    get_runtime,
    get_task_service,
    get_transport_assistant_service,
)
from app.task_runtime.service.task_service import TaskService


class AssistantAttachRequest(BaseModel):
    """只订阅已有 Conversation Run 的 transport 请求。"""

    # assistant-ui resume requests carry the common transport envelope
    # (commands/state/system/tools/callSettings/config). Attach only consumes
    # identity fields and intentionally ignores that envelope.
    model_config = ConfigDict(extra="ignore")

    commands: list[object] = Field(default_factory=list)
    taskId: int | None = Field(default=None, ge=1)
    threadId: str = Field(pattern=r"^task-[1-9][0-9]*$")
    runId: int = Field(ge=1)


@app.post("/assistant")
async def assistant_transport(
    request: AssistantTransportRequest,
    task_service: TaskService = Depends(get_task_service),
    command_service: ConversationRunCommandService = Depends(get_conversation_run_command_service),
    run_executor: ConversationRunExecutor = Depends(get_conversation_run_executor),
    runtime: AgentRuntime = Depends(get_runtime),
    transport_service: TransportAssistantService = Depends(get_transport_assistant_service),
) -> AssistantTransportResponse:
    """接收用户消息并返回 Assistant Transport 状态流。

    参数:
        request: Assistant UI request 请求，当前业务命令为文本 ``add-message``。
        snapshot_service: 负责读取 Task snapshot 的唯一 owner。
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

    try:
        task = task_service.get_task(request.taskId)
    except KeyError:
        _raise_transport_error(
            404,
            "TASK_NOT_FOUND",
            "对话任务不存在，请重新创建对话",
            retryable=False,
            command_id=command.commandId if command is not None else None,
        )

    start_result = None
    if command is None:
        # 空 commands 只表示业务续跑；Assistant UI 的 transport attach 走独立
        # resumeApi，不经过本入口。
        if request.runId is None:
            _raise_transport_error(
                400,
                "RUN_ID_REQUIRED",
                "请先创建运行切片",
                retryable=False,
            )
        if run_executor.is_cancelling(request.runId):
            _raise_transport_error(
                409,
                "RUN_CANCELLING",
                "运行正在取消，请稍后重新提交恢复请求",
                retryable=True,
                run_id=request.runId,
            )
        if run_executor.is_locally_running(request.runId):
            _raise_transport_error(
                409,
                "RUN_ALREADY_RUNNING",
                "对话运行当前正在运行，请使用 attach 重新订阅",
                retryable=True,
                run_id=request.runId,
            )

    if request.workspaceId is not None and task.workspace_id != request.workspaceId:
        _raise_transport_error(
            409,
            "TASK_WORKSPACE_MISMATCH",
            "对话任务不属于当前工作区",
            retryable=False,
            command_id=command.commandId if command is not None else None,
        )
    task_id = task.id

    try:
        if command is None:
            assert request.runId is not None
            start_result = await asyncio.to_thread(
                command_service.resume_latest_run,
                task_id=task_id,
                run_id=request.runId,
            )
        else:
            input_text = "\n".join(part.text for part in command.message.parts)
            provider_id = request.providerId
            model_name = request.modelName
        if command is not None and request.runId is not None:
            if run_executor.is_cancelling(request.runId):
                _raise_transport_error(
                    409,
                    "RUN_CANCELLING",
                    "运行正在取消，请稍后再编辑并重跑",
                    retryable=True,
                    run_id=request.runId,
                )
            start_result = await asyncio.to_thread(
                command_service.edit_or_restart,
                command_id=command.commandId,
                command_type=command.type,
                payload_hash=request.payload_hash(),
                input_text=input_text,
                task_id=task_id,
                run_id=request.runId,
                provider_id=provider_id,
                model_name=model_name,
                reasoning_effort=request.reasoningEffort,
            )
        elif command is not None:
            start_result = await asyncio.to_thread(
                command_service.start_or_attach,
                command_id=command.commandId,
                command_type=command.type,
                payload_hash=request.payload_hash(),
                input_text=input_text,
                provider_id=provider_id,
                model_name=model_name,
                reasoning_effort=request.reasoningEffort,
                task_id=task_id,
            )
        assert start_result is not None
        run = start_result.run
        initial_state = start_result.initial_state

        if start_result.created:
            try:
                await run_executor.start(
                    run.id,
                    lambda execution_run: runtime.execute_run(
                        execution_run,
                        execution_mode=start_result.execution_mode,
                    ),
                )
            except ValueError:
                # 另一个进程内请求已经登记相同 run；本请求只重新订阅。
                log.info(
                    "assistant_transport_executor_already_claimed",
                    extra={
                        "msg": "执行器已被其他请求认领，本请求退化为纯订阅",
                        "data": {"run_id": run.id, "task_id": task_id},
                    },
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
        # hide actionable states such as RUN_CANCELLING and RUN_NOT_RESUMABLE.
        raise
    except ValueError as exc:
        operation_code = (
            "RUN_NOT_RESUMABLE"
            if command is None
            else "RUN_NOT_REPLAYABLE"
            if request.runId is not None
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
    snapshot_service: ConversationTaskSnapshotService = Depends(
        get_conversation_task_snapshot_service
    ),
) -> ConversationStateSnapshot:
    """返回某任务的首屏历史 state（服务端权威对话视图）。

    参数:
        task_id: URL 中的任务标识。
        task_service: 用于校验任务存在性的领域 service（不直接触碰 storage）。
        snapshot_service: Task snapshot 唯一事实源。

    返回:
        与 Assistant Transport state 形状一致的中性 wire state 字典（含 ``messages`` / ``run``）；
        任务无轮次时 ``messages`` 为空数组。

    异常:
        HTTPException: 任务不存在时返回 404（复用 ``task_service.get_task`` 的
        ``KeyError`` 约定，与 ``assistant_transport`` 创建路径一致）。

    副作用:
        不修改 Run、Context 或执行器；snapshot service 可能在发现 Run 已进入终态而
        snapshot 尚未投影时，执行幂等的 snapshot 对账写入，然后返回最终一致的 state。
    """
    # 先校验任务存在：不存在时 ``get_task`` 抛 KeyError → 映射为 404。
    # 不能在投影阶段再判，因为空 task 与不存在 task 在投影层都表现为空 messages。
    try:
        task_service.get_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    state = await snapshot_service.read(task_id)
    log.info(
        "assistant_snapshot_read",
        extra={
            "msg": "读取 Assistant 历史快照",
            "data": {
                "task_id": task_id,
                "run_id": state["run"]["runId"],
                "run_status": state["run"]["status"],
                "message_count": len(state["messages"]),
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

    表现层只做输入校验、调用业务层与异常映射，不再编排「先落库再中断」的业务时序——
    取消编排（进程内取消信号 → 事务性状态转移 → 中断后台 task）已收口到
    ``ConversationRunExecutor.cancel`` 单一入口。HTTP 断连不会调用本端点，重复取消
    不会覆盖已落定的终态。

    参数:
        run_id: Conversation Run 标识。
        run_executor: 取消编排唯一入口（信号标记 + 仲裁落库 + task 中断）。

    返回:
        包含 ``run_id`` 与 ``status`` 的 JSON 对象。

    异常:
        HTTPException: run 不存在时返回 404；run 已处于不可取消终态时返回 409；
        取消编排内部失败时返回 500。

    副作用:
        经执行器先标记进程内取消信号，再落库取消终态，再尽力中断当前进程中的执行任务。
    """
    try:
        cancelled = await run_executor.cancel(run_id, end_reason="user_cancelled")
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
    if not cancelled:
        log.info(
            "conversation_run_cancel_rejected",
            extra={"msg": "Conversation Run 当前状态不允许取消", "data": {"run_id": run_id}},
        )
        raise HTTPException(status_code=409, detail="run is not in a cancellable state")
    log.info(
        "conversation_run_cancelled",
        extra={"msg": "Conversation Run 已取消", "data": {"run_id": run_id}},
    )
    return JSONResponse(content={"run_id": run_id, "status": "cancelled"})
