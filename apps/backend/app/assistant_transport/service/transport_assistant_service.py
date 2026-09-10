"""Assistant Transport 的 run 状态订阅适配。"""

from __future__ import annotations

import asyncio
from typing import NoReturn

from assistant_stream import create_run
from assistant_stream.serialization import AssistantTransportResponse
from fastapi import HTTPException

from app.assistant_transport.request import AddMessageCommand, AssistantTransportRequest
from app.assistant_transport.service.conversation_run_command_service import (
    ConversationRunStartResult,
    RunCommandMode,
)
from app.assistant_transport.service.conversation_task_snapshot_service import (
    ConversationTaskSnapshotService,
)
from app.assistant_transport.service.transport_stream_service import (
    AssistantTransportStreamService,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
    find_run,
)
from app.config.logging.logger import log
from app.models import ConversationRunStatus


class TransportAssistantService:
    """负责 Assistant Transport 入口的 run 前置校验、生命周期编排与响应构造。

    快照订阅与 SSE 编码（stream / subscribe_run_state 等）已拆分到
    ``AssistantTransportStreamService``；本类通过 ``self._stream`` 委托其完成流式响应。
    """

    def __init__(self) -> None:
        """初始化 run 校验、生命周期与流式订阅依赖。"""

        self._snapshots = ConversationTaskSnapshotService()
        from app.service.depends import get_conversation_run_executor

        self.run_executor = get_conversation_run_executor()
        from app.service.depends import get_conversation_run_service

        self._runs = get_conversation_run_service()
        from app.service.depends import get_task_service

        self._tasks = get_task_service()
        from app.service.depends import get_conversation_run_command_service

        self._commands = get_conversation_run_command_service()
        self._stream = AssistantTransportStreamService()

    def build_response(
        self,
        *,
        task_id: int,
        thread_id: str,
        run_id: int,
        state: ConversationStateSnapshot,
    ) -> AssistantTransportResponse:
        """为指定 run 构造统一的 Assistant Transport snapshot response。"""

        try:
            find_run(state, run_id)
        except KeyError as exc:
            raise ValueError(
                f"snapshot run {run_id} does not exist"
            ) from exc
        stream = create_run(
            lambda controller: self._stream.subscribe_run_state(controller, task_id, run_id),
            state=state,
        )
        response = AssistantTransportResponse(stream)
        response.headers["X-Cosir-Task-Id"] = str(task_id)
        response.headers["X-Cosir-Thread-Id"] = thread_id
        return response

    @staticmethod
    def classify_run_command(
        command: AddMessageCommand | None,
        run_id: int | None,
    ) -> RunCommandMode:
        """将 Assistant Transport command batch 归一化为本项目的 Run 模式。

        参数:
            command: 请求中唯一允许的 ``add-message`` 命令；空 batch 时为 ``None``。
            run_id: 请求根部的 Conversation Run 目标标识。

        返回:
            ``new`` 表示新建 Conversation Run；``edit`` 表示原地重置最近 Run 后重新执行；
            ``resume`` 表示恢复可续跑的 cancelled Run。

        异常:
            ValueError: command batch 既没有消息又没有 run_id；正常请求解析已在 Pydantic
                validator 中拒绝该情况，这里只作为接入层的防御性校验。

        副作用:
            无；仅根据已解析的 wire 字段分类，不读取数据库，也不判断 run 是否存在。
        """
        if command is not None:
            return "edit" if run_id is not None else "new"
        if run_id is not None:
            return "resume"
        raise ValueError("run command requires a message or run_id")

    def ensure_run_target(
        self,
        *,
        request: AssistantTransportRequest,
        command: AddMessageCommand | None,
        mode: RunCommandMode,
    ) -> int:
        """校验 assistant 入口 run 前置条件并返回目标 task_id。

        把 ``assistant_api.assistant_transport`` 中散落的任务存在性、resume 前置与
        workspace 归属读校验收敛到 service 层；API 只负责调用本方法并让
        ``_raise_transport_error`` 抛出的 ``HTTPException`` 自然向上传播，
        不再在接入层重复编排这些领域校验。

        参数:
            request: Assistant Transport 请求（提供 taskId / runId / workspaceId）。
            command: 归一化后的唯一 add-message 命令（可能为 None）；其 commandId
                用于结构化错误上下文。
            mode: ``new`` / ``edit`` / ``resume`` 的 Run 模式。

        返回:
            校验通过的目标 task_id。

        异常:
            HTTPException: 经由 ``_raise_transport_error`` 抛出，覆盖 TASK_NOT_FOUND /
                RUN_ID_REQUIRED / RUN_CANCELLING / RUN_ALREADY_RUNNING /
                COMMAND_REQUIRED / TASK_WORKSPACE_MISMATCH。

        副作用:
            仅做读校验与一条 info 日志；不修改 Run / Task / Context。
        """

        command_id = command.commandId if command is not None else None
        try:
            task = self._tasks.get_task(request.taskId)
        except KeyError:
            _raise_transport_error(
                404,
                "TASK_NOT_FOUND",
                "对话任务不存在，请重新创建对话",
                retryable=False,
                command_id=command_id,
            )

        if mode == "resume":
            # 空 commands 只表示业务续跑；Assistant UI 的 transport attach 走独立
            # resumeApi，不经过本入口。
            if request.runId is None:
                _raise_transport_error(
                    400,
                    "RUN_ID_REQUIRED",
                    "请先创建运行切片",
                    retryable=False,
                )
            if self.run_executor.is_cancelling(request.runId):
                _raise_transport_error(
                    409,
                    "RUN_CANCELLING",
                    "运行正在取消，请稍后重新提交恢复请求",
                    retryable=True,
                    run_id=request.runId,
                )
            if self.run_executor.is_locally_running(request.runId):
                _raise_transport_error(
                    409,
                    "RUN_ALREADY_RUNNING",
                    "对话运行当前正在运行，请使用 attach 重新订阅",
                    retryable=True,
                    run_id=request.runId,
                )
        elif mode == "edit":
            if request.runId is None:
                _raise_transport_error(
                    400,
                    "RUN_ID_REQUIRED",
                    "请先创建运行切片",
                    retryable=False,
                )
            if command is None:
                _raise_transport_error(
                    400,
                    "COMMAND_REQUIRED",
                    "编辑请求需要携带 add-message 命令",
                    retryable=False,
                    run_id=request.runId,
                )
            if self.run_executor.is_cancelling(request.runId):
                _raise_transport_error(
                    409,
                    "RUN_CANCELLING",
                    "运行正在取消，请稍后再编辑并重跑",
                    retryable=True,
                    run_id=request.runId,
                )
        else:  # new
            if command is None:
                _raise_transport_error(
                    400,
                    "COMMAND_REQUIRED",
                    "请发送消息后再提交",
                    retryable=False,
                    command_id=command_id,
                )

        if request.workspaceId is not None and task.workspace_id != request.workspaceId:
            _raise_transport_error(
                409,
                "TASK_WORKSPACE_MISMATCH",
                "对话任务不属于当前工作区",
                retryable=False,
                command_id=command_id,
            )

        task_id = task.id
        log.info(
            "assistant_transport_run_command_classified",
            extra={
                "msg": "Assistant Transport 请求已归一化为 Run 模式",
                "data": {
                    "task_id": task_id,
                    "run_id": request.runId,
                    "command_id": command_id,
                    "mode": mode,
                },
            },
        )
        return task_id

    async def prepare_run_start(
        self,
        *,
        task_id: int,
        command: AddMessageCommand | None,
        mode: RunCommandMode,
        request: AssistantTransportRequest,
    ) -> ConversationRunStartResult:
        """在 command service 层原子占用/创建/恢复 run，返回启动编排所需的 start result。

        ``ensure_run_target`` 已完成全部前置校验；本方法只做 run 占用与基线状态装配
        （幂等重连、新建、原地编辑或续跑），不触发 AgentRuntime 执行。真正的执行由
        API 经 ``run_executor.start`` 触发，并由返回的 ``created`` 标志门控。

        参数:
            task_id: 已校验通过的目标任务标识。
            command: 归一化后的唯一 add-message 命令（resume 时为 None）。
            mode: ``new`` / ``edit`` / ``resume`` 的 Run 模式。
            request: 原始 Assistant Transport 请求，提供 runId / payload / 模型等。

        返回:
            ``ConversationRunStartResult``：含已占用或创建的 run、初始快照、
            ``created`` 标志与 ``execution_mode``。

        异常:
            ValueError: command service 领域校验失败（run 不可续跑 / 不可编辑 / 已有
                active run 等），交由 API 的 ``except ValueError`` 翻译为对应 transport 错误。

        副作用:
            仅在 storage 层原子写入 command / run / snapshot baseline，不启动执行器、不发布
            run-status 事件。
        """

        if mode == "resume":
            # ensure_run_target 已校验 run_id 非空；此处 assert 仅做静态类型收窄。
            assert request.runId is not None
            return await asyncio.to_thread(
                self._commands.resume_latest_run,
                task_id=task_id,
                run_id=request.runId,
            )
        # 非 resume 模式 command 必非空（详见 _classify_run_command）；assert 仅类型收窄。
        assert command is not None
        input_text = "\n".join(part.text for part in command.message.parts)
        provider_id = request.providerId
        model_name = request.modelName
        if mode == "edit":
            assert request.runId is not None
            return await asyncio.to_thread(
                self._commands.edit_or_restart,
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
        return await asyncio.to_thread(
            self._commands.start_or_attach,
            command_id=command.commandId,
            command_type=command.type,
            payload_hash=request.payload_hash(),
            input_text=input_text,
            provider_id=provider_id,
            model_name=model_name,
            reasoning_effort=request.reasoningEffort,
            task_id=task_id,
        )

    async def attach_run(
        self,
        *,
        task_id: int,
        thread_id: str,
        run_id: int,
    ) -> AssistantTransportResponse:
        """只订阅一个已有 run 的 canonical snapshot，不启动或恢复执行。

        参数:
            task_id: 任务标识。
            thread_id: Assistant UI thread 标识。
            run_id: 已存在的 Conversation Run 标识。

        返回:
            使用 ``assistant-stream`` 编码的 snapshot subscription 响应。

        异常:
            HTTPException: run 不属于 task、不是当前 latest run、或 snapshot 尚未
                收敛时抛出结构化 transport 错误。

        副作用:
            只注册 snapshot subscriber；不会创建 executor、修改 Run status、写入
            Context 或调用业务 resume。
        """

        try:
            run = self._runs.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        if run.task_id != task_id:
            _raise_transport_error(
                409,
                "RUN_TASK_MISMATCH",
                "运行不属于当前任务",
                retryable=False,
                run_id=run_id,
            )

        state = await self._snapshots.read(task_id)
        if state["current_run_id"] != run_id:
            _raise_transport_error(
                409,
                "RUN_NOT_ATTACHABLE",
                "当前任务的最新快照已不是该运行",
                retryable=True,
                run_id=run_id,
            )
        if run.status not in {
            ConversationRunStatus.PENDING.value,
            ConversationRunStatus.RUNNING.value,
        }:
            _raise_transport_error(
                409,
                "RUN_NOT_ATTACHABLE",
                "只有仍在执行的运行可以建立状态订阅",
                retryable=False,
                run_id=run_id,
            )
        if run.status in {
            ConversationRunStatus.PENDING.value,
            ConversationRunStatus.RUNNING.value,
        } and not self.run_executor.is_locally_running(run_id):
            _raise_transport_error(
                409,
                "RUN_RECOVERY_REQUIRED",
                "本机后端尚未恢复该运行，请先读取最新状态",
                retryable=True,
                run_id=run_id,
            )

        return self.build_response(
            task_id=task_id,
            thread_id=thread_id,
            run_id=run_id,
            state=state,
        )


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
        无；本函数始终抛出 ``HTTPException``，返回类型标注 ``NoReturn`` 供 mypy
        把所有调用点所在的 except 分支识别为不可达路径。

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
