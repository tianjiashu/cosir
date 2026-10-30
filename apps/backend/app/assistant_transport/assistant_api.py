"""Assistant Transport API：Task 级命令、订阅与 snapshot 读取。

命令只对 Task（或消息）寻址，不携带 run_id。本模块只做 wire 映射与 HTTP 错误映射，用例编排全部在
``TransportAssistantService``。用户命令（发送、编辑、重跑、恢复、审批决定）等后端处理完这一轮再
返回：输入问题 400、Task 不存在 404、状态冲突 409，成功 200；只有取消是纯投递（202），是否停下以
snapshot 的 ``activity`` 为准。Run 输出与状态只经 snapshot 交给前端。attach 是跟随 Task 的长订阅，
不触发业务执行。
"""

import asyncio
from collections.abc import Awaitable
from typing import NoReturn

from fastapi import Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app.app import app
from app.assistant_transport.request import (
    DecisionRequest,
    RerunMessageRequest,
    SendMessageRequest,
)
from app.assistant_transport.service.transport_assistant_service import (
    TransportAssistantService,
)
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.assistant_transport.state.snapshot_window import HistoryPage
from app.config.logging.logger import log
from app.core.task_actor import SessionConflictError
from app.models.errors.input_errors import UserInputRejectedError
from app.service.attachment.image_normalizer import ImageNormalizationError
from app.service.depends import get_transport_assistant_service


def _raise_transport_error(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool,
) -> NoReturn:
    """抛出统一的 Assistant Transport HTTP 错误；``message`` 是面向用户的安全提示。"""

    raise HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "retryable": retryable}},
    )


async def _accept(
    task_id: int,
    operation: str,
    call: Awaitable[object],
    status_code: int = 200,
) -> JSONResponse:
    """执行一次 Task 命令，并把输入错误、状态冲突映射为 Transport 错误。"""

    try:
        await call
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except SessionConflictError as exc:
        _raise_transport_error(409, exc.code, str(exc), retryable=exc.retryable)
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
                "msg": "用户输入在投递前被拒",
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
            "msg": "Task 命令已处理",
            "data": {"task_id": task_id, "operation": operation},
        },
    )
    return JSONResponse(content={}, status_code=status_code)


@app.post("/tasks/{task_id}/messages")
async def send_message(
    task_id: int,
    request: SendMessageRequest,
    service: TransportAssistantService = Depends(get_transport_assistant_service),
) -> JSONResponse:
    """发送消息：有活动 Run（含挂起）时排队，当前 Run 结束后一轮一条依次处理；空闲时立即处理。"""

    return await _accept(task_id, "send", service.send(task_id, request))


@app.post("/tasks/{task_id}/queued-messages/{message_id}/send-now")
async def send_queued_message_now(
    task_id: int,
    message_id: int,
    service: TransportAssistantService = Depends(get_transport_assistant_service),
) -> JSONResponse:
    """把排队中的用户消息改为立即处理：运行中并入当前 Run，挂起时唤醒，空闲时开新 Run（已并入时
    409 ``MESSAGE_CONSUMED``）。"""

    return await _accept(
        task_id, "send_queued_now", service.send_queued_now(task_id, message_id)
    )


@app.delete("/tasks/{task_id}/queued-messages/{message_id}")
async def delete_queued_message(
    task_id: int,
    message_id: int,
    service: TransportAssistantService = Depends(get_transport_assistant_service),
) -> JSONResponse:
    """删除排队中的用户消息（已并入时 409 ``MESSAGE_CONSUMED``）。"""

    return await _accept(task_id, "delete_queued", service.delete_queued(task_id, message_id))


@app.post("/tasks/{task_id}/messages/{message_id}/rerun")
async def rerun_message(
    task_id: int,
    message_id: int,
    request: RerunMessageRequest,
    service: TransportAssistantService = Depends(get_transport_assistant_service),
) -> JSONResponse:
    """编辑最新一条已并入的用户消息并重跑：只在 Task 静止时接受（不替用户停止，否则 409
    ``RUN_NOT_STOPPED``），撤回它所在的一轮，编辑后的消息进入新建的 Run。"""

    return await _accept(task_id, "rerun", service.rerun(task_id, message_id, request))


@app.post("/tasks/{task_id}/resume")
async def resume_task(
    task_id: int,
    service: TransportAssistantService = Depends(get_transport_assistant_service),
) -> JSONResponse:
    """恢复 Task：最新 Run 为 cancelled / failed 时从 prepare_input 续跑；不可恢复时 409
    ``RUN_NOT_RESUMABLE``。"""

    return await _accept(task_id, "resume", service.resume(task_id))


@app.post("/tasks/{task_id}/tool-calls/{tool_call_id}/decision")
async def submit_decision(
    task_id: int,
    tool_call_id: str,
    request: DecisionRequest,
    service: TransportAssistantService = Depends(get_transport_assistant_service),
) -> JSONResponse:
    """对一次等待用户决定的工具调用作出决定：同步校验后入队，由 Task 的 actor 唤醒或开新 Run。

    409 ``DECISION_NOT_AWAITED``（调用不在等待决定）/ ``DECISION_ALREADY_SUBMITTED``（已有未处理
    的决定），400 ``DECISION_DATA_INVALID``（载荷不符合工具声明的形状）。"""

    return await _accept(task_id, "decide", service.decide(task_id, tool_call_id, request))


@app.post("/tasks/{task_id}/assistant/attach")
async def assistant_transport_attach(
    task_id: int,
    service: TransportAssistantService = Depends(get_transport_assistant_service),
) -> StreamingResponse:
    """订阅 Task 的 snapshot（长订阅），不触发业务 resume。"""

    try:
        return service.attach(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc


@app.get("/tasks/{task_id}/assistant/state")
async def assistant_transport_state(
    task_id: int,
    service: TransportAssistantService = Depends(get_transport_assistant_service),
) -> ConversationStateSnapshot:
    """返回某任务的首屏历史 state（服务端权威对话视图）；任务不存在时 404。

    冷读时 state service 从 canonical Task、Run 与 Context 记录重建；不修改 Run、Context 或执行器。
    """
    try:
        state = service.get_state(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    activity = state["activity"]
    log.info(
        "assistant_snapshot_read",
        extra={
            "msg": "读取 Assistant 历史快照",
            "data": {
                "task_id": task_id,
                "run_id": activity["runId"] if activity is not None else None,
                "run_status": activity["status"] if activity is not None else None,
                "message_count": len(state["messages"]),
                "message_offset": state["messageOffset"],
            },
        },
    )
    return state


@app.get("/tasks/{task_id}/assistant/messages")
async def assistant_history_page(
    task_id: int,
    end: int = Query(ge=0),
    service: TransportAssistantService = Depends(get_transport_assistant_service),
) -> HistoryPage:
    """完整历史中位置 ``end`` 之前的一页，前端往上翻时取；``end`` 失效（快照已被替换）时 409。"""

    try:
        return service.get_history_page(task_id, end)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except ValueError:
        _raise_transport_error(
            409, "HISTORY_STALE", "对话已更新，请重新加载更早的消息", retryable=False
        )


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: int,
    service: TransportAssistantService = Depends(get_transport_assistant_service),
) -> JSONResponse:
    """停止 Task 当前的执行或挂起的 Run，投递即 202；是否停下以 snapshot 的 ``activity`` 为准。

    按 task 而不是 run 取消：同一 Task 内 Run 串行，命中的永远是当前执行，不依赖前端可能滞后的
    run_id。运行中的 Run 在入口同步打取消信号，终态由 workflow 在检查点落定；挂起（``suspended``）
    的 Run 没有执行者，由 Task 调度 actor 直接迁移为 ``cancelled``；没有可停止的对象时什么也不做。
    重复请求幂等。HTTP 断连不会调用本端点。
    """

    return await _accept(task_id, "cancel", service.cancel(task_id), status_code=202)


@app.post("/tasks/{task_id}/tool-calls/{tool_call_id}/background")
async def background_tool_call(
    task_id: int,
    tool_call_id: str,
    service: TransportAssistantService = Depends(get_transport_assistant_service),
) -> JSONResponse:
    """把一次前台等待中的工具调用转到后台：工具立即以「已转后台」占位闭合，结论稍后回投。

    409 ``TOOL_CALL_NOT_DETACHABLE``：这次调用没有前台等待（已结束、本来就在后台或不支持）。
    结论与转后台同时到达时由登记表裁决，结论先到则本请求 409、工具照常返回结果。
    """

    async def detach() -> None:
        await asyncio.to_thread(service.background_tool_call, task_id, tool_call_id)

    return await _accept(task_id, "background_tool_call", detach())


@app.post("/tasks/{task_id}/tool-calls/{tool_call_id}/cancel")
async def cancel_tool_call(
    task_id: int,
    tool_call_id: str,
    service: TransportAssistantService = Depends(get_transport_assistant_service),
) -> Response:
    """取消一次工具调用，Run 与 Agent 继续执行。

    登记了延迟结果的调用（前台等待或后台工作）撤销登记，不要求 Run 在执行；其余只给 Task 当前
    执行中的调用打工具级取消信号。本端点只表示取消已被接受；工具调用终态以 canonical snapshot
    为准。

    异常:
        HTTPException: 没有可取消的对象（Task 当前没有执行、调用已结束或已标记过取消）时 409。
    """
    if not await asyncio.to_thread(service.cancel_tool_call, task_id, tool_call_id):
        raise HTTPException(
            status_code=409, detail="task has no running execution or call is already cancelled"
        )
    log.info(
        "tool_call_cancelled",
        extra={
            "msg": "工具调用取消信号已接受",
            "data": {"task_id": task_id, "tool_call_id": tool_call_id},
        },
    )
    return JSONResponse(content={"task_id": task_id, "tool_call_id": tool_call_id})
