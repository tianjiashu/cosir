"""Assistant UI Assistant Transport API。

本模块是新的对话传输接入层：接收 assistant-ui ``add-message`` 命令，创建运行切片，
并把服务端 canonical conversation facts 编码为 Assistant Transport state operations。
它不把 assistant-ui 类型传入 core、service 或 storage。
"""

from typing import Any, NoReturn, cast

from assistant_stream import create_run
from assistant_stream.serialization import AssistantTransportResponse
from fastapi import Depends, HTTPException
from app.app import app
from app.assistant_transport.request import (
    AddMessageCommand,
    AssistantTransportRequest,
)

from app.assistant_transport.service.conversation_run_executor import ConversationRunExecutor

from app.assistant_transport.service.conversation_task_snapshot_service import (
    ConversationTaskSnapshotService,
)
from app.assistant_transport.service.transport_assistant_service import TransportAssistantService, \
    ConversationRunStartResult
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot as AssistantTransportState,
)
from app.config.logging.logger import log
from app.service.depends import (
    get_conversation_run_executor,
    get_conversation_run_service,
    get_conversation_task_snapshot_service,
    get_task_service, get_transport_assistant_service,
)
from app.task_runtime.service.task_service import TaskService
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from app.utils.datetime_utils import preview


def _empty_snapshot() -> AssistantTransportState:
    """返回任务首屏使用的固定空 snapshot。"""

    return {
        "messages": [],
        "run": {"runId": None, "status": "idle"},
        "approvals": {},
        "error": None,
    }


def _raise_transport_error(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool,
    command_id: str | None = None,
    run_id: int | None = None,
) -> NoReturn:
    """抛出统一的 Assistant Transport HTTP 错误。

    参数:
        status_code: HTTP 状态码。
        code: 稳定的机器可读错误码。
        message: 面向用户的安全提示，不包含密钥或异常堆栈。
        retryable: 客户端是否可以在修正条件后重试。
        command_id: 可选的 Transport 命令标识。
        run_id: 可选的后端 Conversation Run 标识。

    返回:
        无；本函数始终抛出 ``HTTPException``，返回类型标注 ``NoReturn`` 供
        mypy 把所有调用点所在的 except 分支识别为不可达路径。

    异常:
        HTTPException: 携带统一 ``error`` 对象的 HTTP 异常。

    副作用:
        无。
    """
    error: dict[str, object] = {
        "code": code,
        "message": message,
        "retryable": retryable,
    }
    if command_id is not None:
        error["commandId"] = command_id
    if run_id is not None:
        error["runId"] = run_id
    raise HTTPException(status_code=status_code, detail={"error": error})


@app.post("/assistant")
async def assistant_transport(
    request: AssistantTransportRequest,
    task_service: TaskService = Depends(get_task_service),  
    transport_service: TransportAssistantService = Depends(get_transport_assistant_service),
) -> AssistantTransportResponse:
    """接收用户消息并返回 Assistant Transport 状态流。

    参数:
        request: Assistant UI request 请求，当前业务命令为文本 ``add-message``。
        runtime: 通过依赖注入取得的 AgentRuntime。
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
        command for command in request.commands if isinstance(command, AddMessageCommand)
    )
    input_text = "\n".join(part.text for part in command.message.parts)
    provider_id = request.providerId
    model_name = request.modelName
    if provider_id is None or model_name is None:
        _raise_transport_error(
            400,
            "MODEL_SELECTION_REQUIRED",
            "请先选择模型和模型提供商",
            retryable=False,
            command_id=command.commandId,
        )

    # 创建或获取任务
    task = task_service.get_or_create_task(task_id=request.taskId, workspace_id=request.workspaceId, title=preview(input_text),
                                           creation_command_id=command.commandId, )
    task_id = task.id

    task_space = task_runtime_spaces.get_or_create(task_id)
    task_space_lock = task_space.lock

    acquire = task_space_lock.acquire(blocking=True, timeout=10)

    if not acquire:
        _raise_transport_error(
            409,
            "RUN_ALREADY_STARTED",
            "该任务正在执行，请等待当前运行结束后再发送",
            retryable=False,
            command_id=command.commandId,
        )

    try:
        start_result: ConversationRunStartResult = transport_service.start(
            command_id=command.commandId,
            command_type=command.type,
            payload_hash=request.payload_hash(),
            input_text=input_text,
            provider_id=provider_id,
            model_name=model_name,
            reasoning_effort=request.reasoningEffort,
            task_id=task_id,
        )
        run = start_result.run
        initial_state = start_result.initial_state

        await transport_service.start_executor(run.id)

        stream = create_run(
            lambda controller: transport_service.subscribe_run_state(
                controller,
                task_id,
                run.id,
            ),
            state=initial_state,
        )
        response = AssistantTransportResponse(stream)
        response.headers["X-Cosir-Task-Id"] = str(task_id)
        response.headers["X-Cosir-Thread-Id"] = f"task-{task_id}"
        return response
    except Exception:
        log.exception(
            "assistant_transport_run_start_failed",
            extra={
                "msg": "Assistant Transport 命令与运行切片原子创建失败",
                "data": {"task_id": task_id, "command_id": command.commandId},
            },
        )
        _raise_transport_error(
            500,
            "RUN_START_FAILED",
            "无法创建对话运行，请稍后重试",
            retryable=True,
        )
    finally:
        task_space_lock.release()



@app.get("/tasks/{task_id}/assistant/state")
async def assistant_transport_state(
    task_id: int,
    task_service: Any = Depends(get_task_service),  
    run_service: Any = Depends(get_conversation_run_service),  
    snapshot_service: ConversationTaskSnapshotService = Depends(  
        get_conversation_task_snapshot_service
    ),
) -> dict[str, object]:
    """返回某任务的首屏历史 state（服务端权威对话视图）。

    参数:
        task_id: URL 中的任务标识。
        task_service: 用于校验任务存在性的领域 service（不直接触碰 storage）。
        snapshot_service: Task snapshot 唯一事实源。

    返回:
        与 POST 端点形状一致的中性 wire state 字典（含 ``messages`` / ``run``）；
        任务无轮次时 ``messages`` 为空数组。

    异常:
        HTTPException: 任务不存在时返回 404（复用 ``task_service.get_task`` 的
        ``KeyError`` 约定，与 ``assistant_transport`` 创建路径一致）。

    副作用:
        以只读方式经 service 层读取任务存在性与轮次/消息事实，不写入任何数据。
    """
    # 先校验任务存在：不存在时 ``get_task`` 抛 KeyError → 映射为 404。
    # 不能在投影阶段再判，因为空 task 与不存在 task 在投影层都表现为空 messages。
    try:
        task_service.get_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    state = snapshot_service.ensure_state_snapshot(task_id)
    return cast(dict[str, object], state)


@app.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: int,
    run_executor: ConversationRunExecutor = Depends(get_conversation_run_executor),  
) -> dict[str, object]:
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
        raise HTTPException(status_code=404, detail="run not found") from exc
    except Exception as exc:
        log.exception(
            "cancel_run_failed",
            extra={"msg": "取消 Conversation Run 失败", "data": {"run_id": run_id}},
        )
        raise HTTPException(status_code=500, detail="failed to cancel run") from exc
    if not cancelled:
        raise HTTPException(status_code=409, detail="run is not in a cancellable state")
    return {"run_id": run_id, "status": "cancelled"}
