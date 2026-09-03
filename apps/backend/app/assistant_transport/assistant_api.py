"""Assistant UI Assistant Transport API。

本模块是新的对话传输接入层：接收 assistant-ui ``add-message`` 命令，创建运行切片，
并把服务端 canonical conversation facts 编码为 Assistant Transport state operations。
它不把 assistant-ui 类型传入 core、service 或 storage。
"""

from typing import Any, NoReturn

from assistant_stream import create_run
from assistant_stream.serialization import AssistantTransportResponse
from fastapi import Depends, HTTPException

from app.api.dependencies import (
    get_conversation_command_service,
    get_conversation_mutation_writer,
    get_conversation_run_executor,
    get_conversation_state_service,
    get_runtime,
    get_task_service,
)
from app.app import app
from app.assistant_transport.request import (
    AddMessageCommand,
    AssistantTransportRequest,
)
from app.config.logging.logger import log
from app.core.runtime.runner import AgentRuntime
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot as AssistantTransportState,
)
from app.assistant_transport.service.conversation_command_service import (
    ConversationCommandService,
    ConversationRunStartResult,
)
from app.assistant_transport.service.conversation_mutation_writer import ConversationMutationWriter
from app.assistant_transport.service.conversation_run_executor import ConversationRunExecutor
from app.assistant_transport.service.conversation_run_subscription_service import (
    ConversationRunSubscriptionService,
)
from app.assistant_transport.service.conversation_snapshot_service import ConversationStateMutation
from app.service.task.conversation_state_service import ConversationStateService


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
    runtime: AgentRuntime = Depends(get_runtime),  # noqa: B008
    conversation_state_service: ConversationStateService = Depends(get_conversation_state_service),  # noqa: B008
    run_service: ConversationCommandService = Depends(get_conversation_command_service),  # noqa: B008
    run_executor: ConversationRunExecutor = Depends(get_conversation_run_executor),  # noqa: B008
) -> AssistantTransportResponse:
    """接收用户消息并返回 Assistant Transport 状态流。

    参数:
        request: Assistant UI request 请求，当前业务命令为文本 ``add-message``。
        runtime: 通过依赖注入取得的 AgentRuntime。
        conversation_state_service: 负责读取任务 ConversationStateSnapshot 的 service。
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
    task_id = request.taskId
    command = next(
        command for command in request.commands if isinstance(command, AddMessageCommand)
    )
    start_result: ConversationRunStartResult | None = None
    input_text = "\n".join(part.text for part in command.message.parts)
    bootstrap_state = (
        conversation_state_service.ensure_task_snapshot(task_id)
        if task_id is not None
        else {"messages": [], "run": {"runId": None, "status": "idle"}, "error": None}
    )
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
    try:
        start_result = run_service.start(
            command_id=command.commandId,
            command_type=command.type,
            payload_hash=request.payload_hash(),
            input_text=input_text,
            provider_id=provider_id,
            model_name=model_name,
            reasoning_effort=request.reasoningEffort,
            task_id=request.taskId,
            workspace_id=request.workspaceId,
            initial_state=bootstrap_state,
        )
        if task_id is None:
            if start_result.task is None:
                raise RuntimeError("new conversation did not return its task")
            task_id = start_result.task.id
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

    run = start_result.run
    initial_state: AssistantTransportState = conversation_state_service.build_initial_state(
        task_id, run.id, input_text
    )
    # 立即在端点内启动后台执行，而不是把 Agent 执行绑定在 SSE 回调里：
    # create_run 的回调只有在客户端真正消费响应流时才会执行；若客户端在流建立前
    # 断开（刷新/切页/代理中断），回调永不执行，run 将永久滞留在 pending。
    # 先启动执行器，再用纯订阅回调回流状态，即可让运行生命周期与 HTTP 订阅彻底解耦。
    await _start_executor(run_executor, runtime, run.id)
    # 构造 SSE 状态流响应：
    # - state=initial_state：连接建立后第一帧立即下发首屏快照
    #   （用户消息 + run=pending），前端不白屏。
    # - 回调只做 canonical state 订阅推送，不驱动 Agent（已在上面启动）。
    stream = create_run(
        lambda controller: _subscribe_run_state(
            controller,
            initial_state,
            task_id,
            run.id,
            run_executor,
            conversation_state_service,
        ),
        state=initial_state,
    )
    response = AssistantTransportResponse(stream)
    response.headers["X-Cosir-Task-Id"] = str(task_id)
    response.headers["X-Cosir-Thread-Id"] = f"task-{task_id}"
    return response


async def _start_executor(
    run_executor: ConversationRunExecutor,
    runtime: AgentRuntime,
    run_id: int,
) -> None:
    """认领并启动指定 run 的后台执行；已被其他执行者持有时静默放行。

    参数:
        run_executor: 进程级后台执行器单例。
        runtime: AgentRuntime，用于组装与启动恢复一致的 runner（仅调用 execute_run）。
        run_id: 待执行的 Conversation Run 标识。

    返回:
        无。

    异常:
        HTTPException: ``run_executor.start`` 抛出 ``ValueError`` 以外的异常时
            （如 run 状态认领的持久化失败），先记录含堆栈的错误日志，再抛 500
            RUN_START_FAILED（retryable），与端点创建路径的错误契约一致。
            ``ValueError`` 只表示「run 已被其他执行者认领/正在执行」，按并发
            竞态静默放行，本请求退化为纯订阅；``KeyError``（run 不存在）在两个
            调用点均不可达——主路径的 run 由 ``run_service.start`` 刚在同一
            事务中创建，duplicate 路径的 run 经已有命令读取成功，
            若因并发删除等极端原因出现则归入其他异常统一落日志。

    副作用:
        在当前事件循环注册后台执行 task；HTTP 订阅断开不会取消它。
    """

    def runner(active_run: Any) -> Any:
        """Adapt the executor runner port to ``AgentRuntime.execute_run``."""
        return runtime.execute_run(active_run)

    try:
        await run_executor.start(run_id, runner)
    except ValueError:
        # 并发窗口内已被其他执行者认领：让既有执行者继续，本请求只订阅。
        log.info(
            "assistant_transport_executor_already_claimed",
            extra={
                "msg": "执行器已被其他执行者认领，本请求退化为纯订阅",
                "data": {"run_id": run_id},
            },
        )
    except Exception:
        log.exception(
            "assistant_transport_executor_start_failed",
            extra={
                "msg": "后台执行器启动失败",
                "data": {"run_id": run_id},
            },
        )
        _raise_transport_error(
            500,
            "RUN_START_FAILED",
            "无法启动对话运行，请稍后重试",
            retryable=True,
        )


async def _subscribe_run_state(
    controller: Any,
    initial_state: AssistantTransportState,
    task_id: int,
    run_id: int,
    run_executor: ConversationRunExecutor,
    conversation_state_service: ConversationStateService,
) -> None:
    """按 run 身份订阅任务快照增量并推送给前端，不驱动 Agent。

    供新命令主路径与 duplicate 幂等重试路径复用。仅把已提交事实的状态增量推回
    前端，不调用 run_executor.start。
    """
    subscription = ConversationRunSubscriptionService(conversation_state_service)

    async def is_terminal() -> bool:
        """返回既有 run 是否已进入终态。"""
        status = await run_executor.status(run_id)
        if status is None:
            snapshot = conversation_state_service.build_run_state(task_id, run_id)
            return snapshot["run"]["status"] in {"completed", "failed", "cancelled"}
        return status.status in {"completed", "failed", "cancelled"}

    async for snapshot in subscription.stream(
        task_id,
        run_id,
        lambda: controller.is_cancelled,
        is_terminal=is_terminal,
    ):
        for mutation in snapshot.mutations:
            _apply_state_mutation(controller, mutation)


def _apply_state_mutation(controller: Any, mutation: ConversationStateMutation) -> None:
    """把中性快照 mutation 适配为 assistant-stream StateProxy 操作。"""

    if not mutation.path:
        controller.state = mutation.value
        return
    if mutation.kind == "append-text":
        controller.append_state_text(list(mutation.path), str(mutation.value))
        return
    target = controller.state
    for key in mutation.path[:-1]:
        target = target[key]
    target[mutation.path[-1]] = mutation.value


@app.get("/tasks/{task_id}/assistant/state")
async def assistant_transport_state(
    task_id: int,
    task_service: Any = Depends(get_task_service),  # noqa: B008
    conversation_state_service: Any = Depends(get_conversation_state_service),  # noqa: B008
) -> dict:
    """返回某任务的首屏历史 state（服务端权威对话视图）。

    参数:
        task_id: URL 中的任务标识。
        task_service: 用于校验任务存在性的领域 service（不直接触碰 storage）。
        conversation_state_service: 把任务轮次事实投影为中性对话视图的 service。

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
    conversation_state_service.ensure_task_snapshot(task_id)
    return conversation_state_service.build_initial_history_state(task_id)


@app.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: int,
    mutation_writer: ConversationMutationWriter = Depends(get_conversation_mutation_writer),  # noqa: B008
    run_executor: ConversationRunExecutor = Depends(get_conversation_run_executor),  # noqa: B008
) -> dict[str, object]:
    """显式取消一个 Conversation Run。

    ``run_id`` 当前与持久化 ``ConversationRun.id`` 一一对应，但该映射只在 API 适配层使用；
    持久化条件状态转移先落定取消事实，再向进程内执行器发出协作取消信号。HTTP
    断连不会调用本端点，重复取消不会覆盖已落定的终态。

    参数:
        run_id: Conversation Run 标识。
        mutation_writer: 提供事务性 active → cancelled 状态转移的服务。
        run_executor: 向当前进程中的执行者发出取消信号的服务。

    返回:
        包含 ``run_id`` 与 ``status`` 的 JSON 对象。

    异常:
        HTTPException: run 不存在时返回 404；run 已处于不可取消终态时返回 409。

    副作用:
        先写入持久化取消状态，再尽力中断当前进程中的执行任务。
    """
    try:
        cancellation = mutation_writer.cancel_run(run_id, end_reason="user_cancelled")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except Exception as exc:
        log.exception(
            "cancel_run_failed",
            extra={"msg": "取消 Conversation Run 失败", "data": {"run_id": run_id}},
        )
        raise HTTPException(status_code=500, detail="failed to cancel run") from exc
    if cancellation is None:
        raise HTTPException(status_code=409, detail="run is not in a cancellable state")
    await run_executor.cancel(run_id)
    return {"run_id": run_id, "status": "cancelled"}
