"""Assistant UI Assistant Transport API。

本模块是新的对话传输接入层：接收 assistant-ui ``add-message`` 命令，创建运行切片，
并把服务端 canonical conversation facts 编码为 Assistant Transport state operations。
它不把 assistant-ui 类型传入 core、service 或 storage。
"""

from typing import Any, NoReturn

from assistant_stream import create_run
from assistant_stream.serialization import AssistantTransportResponse
from fastapi import Depends, HTTPException, Query

from app.api.dependencies import (
    get_conversation_mutation_writer,
    get_conversation_run_executor,
    get_conversation_run_service,
    get_conversation_state_service,
    get_runtime,
    get_task_service,
)
from app.api.transport.assistant_state import AssistantTransportState
from app.api.transport.assistant_transport_request import (
    AddMessageCommand,
    AssistantTransportRequest,
)
from app.app import app
from app.config.logging.logger import log
from app.core.runtime.runner import AgentRuntime
from app.service.task.conversation_mutation_writer import ConversationMutationWriter
from app.service.task.conversation_run_executor import ConversationRunExecutor
from app.service.task.conversation_run_service import (
    ActiveConversationRunError,
    CommandDuplicateError,
    CommandPayloadConflictError,
    ConversationRunService,
    ConversationRunStartResult,
)
from app.service.task.conversation_run_subscription_service import (
    ConversationRunSubscriptionService,
)
from app.service.task.conversation_state_service import ConversationStateService


def _raise_transport_error(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool,
    command_id: str | None = None,
    turn_id: int | None = None,
) -> NoReturn:
    """抛出统一的 Assistant Transport HTTP 错误。

    参数:
        status_code: HTTP 状态码。
        code: 稳定的机器可读错误码。
        message: 面向用户的安全提示，不包含密钥或异常堆栈。
        retryable: 客户端是否可以在修正条件后重试。
        command_id: 可选的 Transport 命令标识。
        turn_id: 可选的后端 Turn 标识。

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
    if turn_id is not None:
        error["turnId"] = turn_id
    raise HTTPException(status_code=status_code, detail={"error": error})


@app.post("/assistant")
async def assistant_transport(
    request: AssistantTransportRequest,
    runtime: AgentRuntime = Depends(get_runtime),
    conversation_state_service: ConversationStateService = Depends(get_conversation_state_service),
    run_service: ConversationRunService = Depends(get_conversation_run_service),
    run_executor: ConversationRunExecutor = Depends(get_conversation_run_executor),
) -> AssistantTransportResponse:
    """接收用户消息并返回 Assistant Transport 状态流。

    参数:
        request: Assistant UI transport 请求，当前业务命令为文本 ``add-message``。
        runtime: 通过依赖注入取得的 AgentRuntime。
        conversation_state_service: 把任务轮次事实投影为本轮运行基线 state 的 service。
        run_service: 在一个事务中占用 command 并创建、绑定 Turn 的 service。
        run_executor: 进程级后台执行器，负责驱动 AgentRuntime 执行。

    返回:
        使用 ``assistant-stream`` 编码的 ``text/event-stream`` 响应。

    异常:
        HTTPException: 请求任务不存在、命令冲突或运行切片创建失败时抛出。

    副作用:
        创建一个 pending turn，并立即启动后台 Agent 执行（与 HTTP 订阅解耦）；
        运行期间更新数据库和 canonical conversation facts。
    """
    task_id = request.taskId
    command = next(
        command for command in request.commands if isinstance(command, AddMessageCommand)
    )
    start_result: ConversationRunStartResult | None = None
    input_text = "\n".join(part.text for part in command.message.parts)
    try:
        start_result = run_service.start(
            task_id=task_id,
            command_id=command.commandId,
            command_type=command.type,
            payload_hash=request.payload_hash(),
            input_text=input_text,
            provider_id=request.providerId,
            model_name=request.modelName,
            reasoning_effort=request.reasoningEffort,
        )
    except KeyError:
        _raise_transport_error(404, "TASK_NOT_FOUND", "任务不存在", retryable=False)
    except CommandPayloadConflictError as exc:
        _raise_transport_error(
            409,
            "COMMAND_PAYLOAD_CONFLICT",
            str(exc),
            retryable=False,
            command_id=command.commandId,
        )
    except CommandDuplicateError:
        # A lost response is a normal retry case: the original request may
        # already have created and started the turn. Reattach to that run
        # instead of returning an unrecoverable duplicate error.
        try:
            turn = run_service.get_existing_turn(
                task_id,
                command.commandId,
                request.payload_hash(),
            )
            state = conversation_state_service.build_run_state(task_id, turn.id)
        except KeyError:
            _raise_transport_error(
                404,
                "COMMAND_NOT_FOUND",
                "找不到待恢复的对话运行，请重新发送",
                retryable=True,
                command_id=command.commandId,
            )
        except CommandPayloadConflictError as exc:
            _raise_transport_error(
                409,
                "COMMAND_PAYLOAD_CONFLICT",
                str(exc),
                retryable=False,
                command_id=command.commandId,
            )
        # If the original request was accepted but disconnected before the
        # executor task was registered, merely subscribing would leave a
        # pending run stranded. Reclaim and execute it in that case. An
        # already active executor must only be subscribed to, otherwise two
        # workers could race on the same run.
        executor_status = await run_executor.status(turn.id)
        if executor_status is None and turn.status in {"pending", "running"}:
            await _start_executor_or_conflict(
                run_executor, runtime, turn.id
            )
        return AssistantTransportResponse(
            create_run(
                lambda controller: _subscribe_run_state(
                    controller,
                    state,
                    task_id,
                    turn.id,
                    run_executor,
                    conversation_state_service,
                ),
                state=state,
            )
        )
    except ActiveConversationRunError as exc:
        _raise_transport_error(409, "RUN_ALREADY_ACTIVE", str(exc), retryable=True)
    except ValueError as exc:
        _raise_transport_error(400, "RUN_REQUEST_INVALID", str(exc), retryable=False)
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

    turn = start_result.turn
    initial_state: AssistantTransportState = conversation_state_service.build_initial_state(
        task_id, turn.id, input_text
    )
    # 立即在端点内启动后台执行，而不是把 Agent 执行绑定在 SSE 回调里：
    # create_run 的回调只有在客户端真正消费响应流时才会执行；若客户端在流建立前
    # 断开（刷新/切页/代理中断），回调永不执行，turn 将永久滞留在 pending，
    # 之后该 task 的所有新消息都会被 409 RUN_ALREADY_ACTIVE 卡死。先启动执行器，
    # 再用纯订阅回调回流状态，即可让运行生命周期与 HTTP 订阅彻底解耦。
    await _start_executor_or_conflict(run_executor, runtime, turn.id)
    # 构造 SSE 状态流响应：
    # - state=initial_state：连接建立后第一帧立即下发首屏快照
    #   （用户消息 + run=pending），前端不白屏。
    # - 回调只做 canonical state 订阅推送，不驱动 Agent（已在上面启动）。
    stream = create_run(
        lambda controller: _subscribe_run_state(
            controller,
            initial_state,
            task_id,
            turn.id,
            run_executor,
            conversation_state_service,
        ),
        state=initial_state,
    )
    return AssistantTransportResponse(stream)


async def _start_executor_or_conflict(
    run_executor: ConversationRunExecutor,
    runtime: AgentRuntime,
    turn_id: int,
) -> None:
    """认领并启动指定 run 的后台执行；已被其他执行者持有时静默放行。

    参数:
        run_executor: 进程级后台执行器单例。
        runtime: AgentRuntime，用于组装与启动恢复一致的 runner（仅调用 run_turn）。
        turn_id: 待执行的 Conversation Run（Turn）标识。

    返回:
        无。

    异常:
        HTTPException: ``run_executor.start`` 抛出 ``ValueError`` 以外的异常时
            （如租约认领的持久化失败），先记录含堆栈的错误日志，再抛 500
            RUN_START_FAILED（retryable），与端点创建路径的错误契约一致。
            ``ValueError`` 只表示「run 已被其他执行者认领/正在执行」，按并发
            竞态静默放行，本请求退化为纯订阅；``KeyError``（turn 不存在）在两个
            调用点均不可达——主路径的 turn 由 ``run_service.start`` 刚在同一
            事务中创建，duplicate 路径的 turn 经 ``get_existing_turn`` 读取成功，
            若因并发删除等极端原因出现则归入其他异常统一落日志。

    副作用:
        在当前事件循环注册后台执行 task；HTTP 订阅断开不会取消它。
    """

    def runner(active_turn: Any) -> Any:
        """Adapt the executor runner port to ``AgentRuntime.run_turn``."""
        return runtime.run_turn(active_turn)

    try:
        await run_executor.start(turn_id, runner)
    except ValueError:
        # 并发窗口内已被其他执行者认领：让既有执行者继续，本请求只订阅。
        log.info(
            "assistant_transport_executor_already_claimed",
            extra={
                "msg": "执行器已被其他执行者认领，本请求退化为纯订阅",
                "data": {"turn_id": turn_id},
            },
        )
    except Exception:
        log.exception(
            "assistant_transport_executor_start_failed",
            extra={
                "msg": "后台执行器启动失败",
                "data": {"turn_id": turn_id},
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
    after_revision: int = -1,
) -> None:
    """按 run 身份订阅 Turn 的 canonical state 增量并推送给前端，不驱动 Agent。

    供三类调用方复用：新命令主路径（执行器已在端点内启动）、duplicate 幂等
    重恢复路径与 ``/assistant/stream`` 续接端点。仅把已提交事实的状态增量推回
    前端，不调用 run_executor.start。
    """
    del initial_state
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
        after_revision=after_revision,
    ):
        controller.state = snapshot


@app.get("/tasks/{task_id}/assistant/state")
async def assistant_transport_state(
    task_id: int,
    task_service: Any = Depends(get_task_service),
    conversation_state_service: Any = Depends(get_conversation_state_service),
) -> dict:
    """返回某任务的首屏历史 state（服务端权威对话视图）。

    参数:
        task_id: URL 中的任务标识。
        task_service: 用于校验任务存在性的领域 service（不直接触碰 storage）。
        conversation_state_service: 把任务轮次事实投影为中性对话视图的 service。

    返回:
        与 POST 端点形状一致的中性 wire state 字典（含 ``messages`` / ``run`` /
        ``revision``）；任务无轮次时 ``messages`` 为空数组。

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
    return conversation_state_service.build_initial_history_state(task_id)


@app.get("/tasks/{task_id}/runs/{run_id}/assistant/stream")
async def assistant_transport_resume(
    task_id: int,
    run_id: int,
    after_revision: int = Query(default=-1, ge=-1),
    conversation_state_service: ConversationStateService = Depends(get_conversation_state_service),
    run_executor: ConversationRunExecutor = Depends(get_conversation_run_executor),
) -> AssistantTransportResponse:
    """按 task/run 身份从指定 revision 订阅 canonical state。"""
    state = conversation_state_service.build_run_state(task_id, run_id)
    if after_revision >= state["revision"]:
        # 仍返回当前快照；订阅服务下一轮只发送 revision 前进的事实。
        state = conversation_state_service.build_run_state(task_id, run_id)
    return AssistantTransportResponse(
        create_run(
            lambda controller: _subscribe_run_state(
                controller,
                state,
                task_id,
                run_id,
                run_executor,
                conversation_state_service,
                after_revision,
            ),
            state=state,
        )
    )


@app.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: int,
    mutation_writer: ConversationMutationWriter = Depends(get_conversation_mutation_writer),
    run_executor: ConversationRunExecutor = Depends(get_conversation_run_executor),
) -> dict[str, object]:
    """显式取消一个 Conversation Run。

    ``run_id`` 当前与持久化 ``Turn.id`` 一一对应，但该映射只在 API 适配层使用；
    持久化条件状态转移先落定取消事实，再向进程内执行器发出协作取消信号。HTTP
    断连不会调用本端点，重复取消不会覆盖已落定的终态。

    参数:
        run_id: Conversation Run 标识；当前实现对应 Turn 标识。
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
    return {"run_id": run_id, "status": "cancelled", "revision": cancellation.revision}
