"""Assistant Transport API：Task 级命令、订阅与 snapshot 读取。

命令只对 Task（或消息）寻址，不携带 run_id，调度规则全部在 ``WorkflowSession``；本模块只做 wire
映射与错误映射。attach 只订阅 Task 当前 Run，不触发业务执行。
"""

from collections.abc import Awaitable

from fastapi import Depends, HTTPException, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app.app import app
from app.assistant_transport.request import (
    EditQueuedMessageRequest,
    RerunMessageRequest,
    SendMessageRequest,
    TeamDecisionRequest,
)
from app.assistant_transport.response.command_accepted import CommandAccepted
from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.assistant_transport.service.transport_assistant_service import (
    TransportAssistantService,
    _raise_transport_error,
)
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.config.logging.logger import log
from app.models.errors.input_errors import UserInputRejectedError
from app.service.attachment.image_normalizer import ImageNormalizationError
from app.service.conversation_run.workflow_session import (
    SessionConflictError,
    SessionReceipt,
    WorkflowSession,
)
from app.service.depends import (
    get_conversation_task_state_service,
    get_task_service,
    get_transport_assistant_service,
    get_workflow_session,
)
from app.task_runtime.service.task_service import TaskService


async def _accept(
    task_id: int,
    operation: str,
    call: Awaitable[SessionReceipt],
    state_service: ConversationTaskStateService,
) -> CommandAccepted:
    """执行一次 Task 命令，并把领域结果与异常统一映射为回执或 Transport 错误。"""

    try:
        receipt = await call
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except SessionConflictError as exc:
        _raise_transport_error(409, exc.code, str(exc), retryable=True)
    except ImageNormalizationError as exc:
        _raise_transport_error(
            503 if exc.code == "ATTACHMENT_STORAGE_UNAVAILABLE" else 400,
            exc.code,
            exc.message,
            retryable=exc.code in {"IMAGE_CONVERSION_FAILED", "ATTACHMENT_STORAGE_UNAVAILABLE"},
        )
    except UserInputRejectedError as exc:
        # 输入问题要用户自己改：400 + 具体原因，不要复用「状态冲突、刷新重试」那种误导文案。
        # ``str(exc)`` 是只进日志的诊断细节（附件 id、缺失资源），线上只回 ``message``。
        log.warning(
            "task_command_input_rejected",
            extra={
                "msg": "用户输入在入队前被拒",
                "data": {
                    "task_id": task_id,
                    "operation": operation,
                    "code": exc.code,
                    "detail": str(exc),
                },
            },
        )
        _raise_transport_error(400, exc.code, exc.message, retryable=False)
    except ValueError as exc:
        log.warning(
            "task_command_rejected",
            extra={
                "msg": "Task 命令领域校验失败，已使用安全错误文案",
                "data": {"task_id": task_id, "operation": operation, "error": str(exc)},
            },
        )
        _raise_transport_error(
            409, "TASK_STATE_CONFLICT", "当前对话状态不允许执行该操作，请刷新后重试", retryable=True
        )
    log.info(
        "task_command_accepted",
        extra={
            "msg": "Task 命令已接受",
            "data": {"task_id": task_id, "operation": operation, "queued": receipt.queued},
        },
    )
    return CommandAccepted(state=state_service.get_state(task_id), queued=receipt.queued)


@app.post("/tasks/{task_id}/messages", response_model=CommandAccepted)
async def send_message(
    task_id: int,
    request: SendMessageRequest,
    session: WorkflowSession = Depends(get_workflow_session),
    state_service: ConversationTaskStateService = Depends(get_conversation_task_state_service),
) -> CommandAccepted:
    """发送消息：有活动 Run 时按 ``delivery`` 立即并入或排到本轮之后，否则新开 Run。"""

    return await _accept(
        task_id,
        "send",
        session.submit(
            task_id,
            request.to_run_command(),
            model_config_id=request.modelConfigId,
            reasoning_effort=request.reasoningEffort,
            delivery=request.delivery,
        ),
        state_service,
    )


@app.patch("/tasks/{task_id}/queued-messages/{message_id}", response_model=CommandAccepted)
async def edit_queued_message(
    task_id: int,
    message_id: int,
    request: EditQueuedMessageRequest,
    session: WorkflowSession = Depends(get_workflow_session),
    state_service: ConversationTaskStateService = Depends(get_conversation_task_state_service),
) -> CommandAccepted:
    """原地编辑排队中的用户消息，不影响 Run；消息已被并入时 409 ``MESSAGE_CONSUMED``。"""

    return await _accept(
        task_id,
        "edit_queued",
        session.edit_queued(task_id, message_id, request.to_run_command()),
        state_service,
    )


@app.post("/tasks/{task_id}/messages/{message_id}/rerun", response_model=CommandAccepted)
async def rerun_message(
    task_id: int,
    message_id: int,
    request: RerunMessageRequest,
    session: WorkflowSession = Depends(get_workflow_session),
    state_service: ConversationTaskStateService = Depends(get_conversation_task_state_service),
) -> CommandAccepted:
    """编辑最新一条已并入的用户消息并重跑：运行中先停止当前 Run，它及其后 context 移出，编辑后
    的消息进入新建的 Run。"""

    return await _accept(
        task_id,
        "rerun",
        session.rerun(
            task_id,
            message_id,
            request.to_run_command(),
            model_config_id=request.modelConfigId,
            reasoning_effort=request.reasoningEffort,
        ),
        state_service,
    )


@app.post("/tasks/{task_id}/resume", response_model=CommandAccepted)
async def resume_task(
    task_id: int,
    session: WorkflowSession = Depends(get_workflow_session),
    state_service: ConversationTaskStateService = Depends(get_conversation_task_state_service),
) -> CommandAccepted:
    """恢复 Task：最新 Run 为 cancelled / failed 时从模型节点续跑。"""

    return await _accept(task_id, "resume", session.resume(task_id), state_service)


@app.post("/tasks/{task_id}/decisions", response_model=CommandAccepted)
async def submit_decision(
    task_id: int,
    request: TeamDecisionRequest,
    state_service: ConversationTaskStateService = Depends(get_conversation_task_state_service),
) -> CommandAccepted:
    """对待确认的 Agent Team 方案作出决定：后端确定性执行（批准即后台运行 Team），再以消息唤醒
    主 Agent。"""
    from app.service.agent_team.agent_team_decision_service import AgentTeamDecisionService

    return await _accept(
        task_id,
        "decide",
        AgentTeamDecisionService().submit(
            task_id, request.teamRunId, request.decision, request.data
        ),
        state_service,
    )


@app.post("/tasks/{task_id}/assistant/attach")
async def assistant_transport_attach(
    task_id: int,
    task_service: TaskService = Depends(get_task_service),
    transport_service: TransportAssistantService = Depends(get_transport_assistant_service),
) -> StreamingResponse:
    """重新订阅 Task 当前 Run，不触发业务 resume。"""

    try:
        task_service.get_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    return transport_service.attach(task_id)


@app.get("/tasks/{task_id}/assistant/state")
async def assistant_transport_state(
    task_id: int,
    task_service: TaskService = Depends(get_task_service),
    state_service: ConversationTaskStateService = Depends(get_conversation_task_state_service),
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


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: int,
    session: WorkflowSession = Depends(get_workflow_session),
) -> Response:
    """停止 Task 当前的执行。

    按 task 而不是 run 取消：同一 Task 内 Run 串行，命中的永远是当前执行，不依赖前端可能
    滞后的 run_id。运行中的 Run 只打取消信号，终态由 workflow 在检查点落定，前端继续以
    canonical snapshot 为准；挂起（``suspended``）的 Run 没有执行者，由 Task 调度 actor 直接
    迁移为 ``cancelled``。执行收尾前的重复请求幂等返回 200。HTTP 断连不会调用本端点。

    异常:
        HTTPException: task 不存在时 404；Task 当前没有可停止的执行时 409；内部失败时 500。
    """
    try:
        accepted = await session.cancel(task_id)
    except KeyError as exc:
        log.warning(
            "task_cancel_not_found",
            extra={"msg": "取消请求未找到 Task", "data": {"task_id": task_id}},
        )
        raise HTTPException(status_code=404, detail="task not found") from exc
    except Exception as exc:
        log.exception(
            "task_cancel_failed",
            extra={"msg": "停止 Task 执行失败", "data": {"task_id": task_id}},
        )
        raise HTTPException(status_code=500, detail="failed to cancel task") from exc
    if not accepted:
        log.info(
            "task_cancel_rejected",
            extra={"msg": "Task 当前没有可停止的执行", "data": {"task_id": task_id}},
        )
        raise HTTPException(status_code=409, detail="task has no cancellable execution")
    log.info(
        "task_cancel_accepted",
        extra={"msg": "Task 取消信号已接受", "data": {"task_id": task_id}},
    )
    return JSONResponse(content={"task_id": task_id, "cancelled": True})


@app.post("/tasks/{task_id}/tool-calls/{tool_call_id}/cancel")
async def cancel_tool_call(
    task_id: int,
    tool_call_id: str,
    session: WorkflowSession = Depends(get_workflow_session),
) -> Response:
    """只中止 Task 当前执行中的一次工具调用，Run 与 Agent 继续执行。

    工具级取消信号仍按 ``(run_id, tool_call_id)`` 记录，run_id 由 Task 当前执行解析。本端点只
    表示信号已被接受；工具调用终态由工具执行层与 workflow 落定，前端以 canonical snapshot 为准。

    异常:
        HTTPException: Task 当前没有执行，或同一调用已标记过取消时 409。
    """
    run_id = session.cancel_tool_call(task_id, tool_call_id)
    if run_id is None:
        raise HTTPException(
            status_code=409, detail="task has no running execution or call is already cancelled"
        )
    log.info(
        "tool_call_cancelled",
        extra={
            "msg": "工具调用取消信号已接受",
            "data": {"task_id": task_id, "run_id": run_id, "tool_call_id": tool_call_id},
        },
    )
    return JSONResponse(content={"task_id": task_id, "tool_call_id": tool_call_id})
