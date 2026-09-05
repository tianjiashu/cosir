"""Conversation command 与 Conversation Run 的持久化编排。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.assistant_transport.service.conversation_task_snapshot_service import (
    ConversationTaskSnapshotService,
    SnapshotChange,
)
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
)
from app.config.logging.logger import log
from app.core.workflows.event import RunInitializedEvent, UserInputAppendedEvent
from app.models import ConversationRunRecord
from app.models.conversation_command_record import ConversationCommandRecord
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.service import depends as service_depends
from app.storage.store_engines import main_session_factory


@dataclass(frozen=True)
class ConversationRunStartResult:
    """一次 Assistant Transport 命令的持久化启动结果。

    命令重复（同 command_id 且同 payload）由 ``start`` 直接抛异常，因此本结果只表示首次
    成功占用并创建绑定的场景。
    """

    command: ConversationCommandRecord
    run: ConversationRunRecord
    initial_state: ConversationStateSnapshot | None = None


class TransportAssistantService:
    def __init__(self) -> None:
        """初始化命令与轮次编排依赖。"""

        self._command = service_depends.get_conversation_command_crud()
        self._conversation_run = service_depends.get_conversation_run_service()
        self._event_projector = service_depends.get_conversation_event_projector()
        self._snapshots = ConversationTaskSnapshotService()
        self._task = service_depends.get_task_service()
        from app.api.dependencies import get_runtime
        from app.service.depends import get_conversation_run_executor

        self.run_executor = get_conversation_run_executor()
        self.runtime = get_runtime()

    def start(
            self,
            command_id: str,
            command_type: str,
            payload_hash: str,
            input_text: str,
            provider_id: int,
            model_name: str,
            reasoning_effort: str | None = None,
            task_id: int | None = None,
    ) -> ConversationRunStartResult:
        """占用命令并创建、绑定 Conversation Run。

        参数:
            command_id: Assistant Transport 命令幂等标识。
            command_type: Transport 命令类型。
            payload_hash: 命令业务载荷指纹。
            input_text: 用户输入文本。
            provider_id: 模型厂商标识。
            model_name: 模型名称。
            reasoning_effort: 可选的推理深度。
            task_id: 续接任务的标识；``None`` 表示新建对话。
            workspace_id: 新建对话所属工作区；仅 ``task_id is None`` 时生效。

        返回:
            新建或续接场景的 command、run 与可选 task 记录。

        异常:
            KeyError: 任务或工作区不存在。
            ValueError: Task 已有 active Run 或命令状态非法。
            RuntimeError: command_id 已被占用或 payload 冲突。
            sqlalchemy.exc.SQLAlchemyError: 持久化失败。

        副作用:
            command、run 和 snapshot baseline 使用各自明确的持久化边界；context 在 Agent
            runtime 真正开始处理用户消息时独立写入。
        """

        existing = self._command.get(task_id, command_id)
        if existing is not None:
            if existing.payload_hash != payload_hash:
                raise RuntimeError(
                    f"command {command_id!r} already exists with a different payload"
                )
            raise RuntimeError(
                f"command {command_id!r} was already submitted with the same payload"
            )

        # 已有 Task 使用其进程内锁；新 Task 尚不存在可锁定的 Task id，依靠
        # BEGIN IMMEDIATE 与数据库唯一约束保护创建事务。
        with main_session_factory().begin() as session:
            has_active_run = self._conversation_run.have_run_in_runing(
                task_id, session
            )
            if has_active_run:
                raise ValueError(f"task {task_id} already has an active run")

            run = self._conversation_run.create_run(
                task_id=task_id,
                input_text=input_text,
                agent_id="main_agent",
                provider_id=provider_id,
                model_name=model_name,
                reasoning_effort=reasoning_effort,
                session=session,
            )
            command = self._command.create(
                task_id=task_id,
                command_id=command_id,
                command_type=command_type,
                payload_hash=payload_hash,
                run_id=run.id,
            )
            snapshot = self._snapshots.ensure_state_snapshot(task_id, session)
            result = ConversationRunStartResult(command, run, snapshot)

        self._event_projector.process(RunInitializedEvent(task_id=task_id, run_id=result.run.id))
        self._event_projector.process(
            UserInputAppendedEvent(task_id=task_id, run_id=result.run.id, text=input_text)
        )
        return result

    async def start_executor(
            self,
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
            return self.runtime.execute_run(active_run)

        try:
            await self.run_executor.start(run_id, runner)
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
            from app.assistant_transport.assistant_api import _raise_transport_error

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

    async def stream(
            self,
            task_id: int,
            run_id: int,
            is_cancelled: Callable[[], bool],
            is_terminal: Callable[[], Awaitable[bool]] | None = None,
            poll_interval: float = 0.05,
            idle_timeout: float = 5.0,
    ) -> AsyncIterator[SnapshotChange]:
        """订阅已提交的局部 mutation，直到 run 进入终态、客户端取消或空闲超时。

        采用 push 模型：经 ``ConversationTaskSnapshotService.subscribe`` 注册进程内队列，
        ``mutate`` 提交后通过 ``call_soon_threadsafe`` 推送 ``SnapshotChange``，无需 revision
        增量比对。订阅前已提交的状态（含 run baseline）经一次 root ``set`` 变更回填，避免
        漏推；订阅与回填读取之间无 ``await`` 切换，故不会与后续队列变更重复应用。外部调用方
        负责把每个 ``SnapshotChange.mutations`` 应用到自己的状态投影（如 Assistant UI thread）。

        Args:
            task_id: 订阅所属的任务 ID。
            run_id: 订阅的 run ID。
            is_cancelled: 客户端取消判定，返回 True 时立即停止迭代。
            is_terminal: 可选；返回 True 时结束迭代。省略时退化为永不主动结束，
                仅靠取消、runId 不匹配或空闲超时终止。
            poll_interval: 单次队列等待超时，兼作取消/终态判定轮询粒度，单位秒。
            idle_timeout: 连续无变更的最长等待，超时后结束迭代。

        Yields:
            SnapshotChange: 订阅前基线的一次性回填 + 订阅后逐个推送的已提交变更。
        """

        queue, unsubscribe = self._snapshots.subscribe(task_id)
        try:
            initial = self._snapshots.ensure_state_snapshot(task_id)
            yield SnapshotChange(
                task_id,
                initial,
                (ConversationStateMutation("set", (), initial),),
            )

            idle_elapsed = 0.0
            while True:
                if is_cancelled():
                    return
                if is_terminal is not None and await is_terminal():
                    return

                try:
                    change = await asyncio.wait_for(queue.get(), poll_interval)
                except TimeoutError:
                    idle_elapsed += poll_interval
                    if idle_elapsed >= idle_timeout:
                        return
                    continue

                if change.state["run"]["runId"] != run_id:
                    return
                yield change
                idle_elapsed = 0.0
        finally:
            unsubscribe()

    async def subscribe_run_state(
            self,
            controller: Any,
            task_id: int,
            run_id: int,
    ) -> None:
        """按 run 身份订阅任务快照增量并推送给前端，不驱动 Agent。

        供新命令主路径与 duplicate 幂等重试路径复用。仅把已提交事实的状态增量推回
        前端，不调用 run_executor.start。
        """

        async def is_terminal() -> bool:
            """返回既有 run 是否已进入终态。"""
            status = await self.run_executor.status(run_id)
            if status is None:
                snapshot = self._snapshots.ensure_state_snapshot(task_id)
                return snapshot["run"]["status"] in {
                    "completed",
                    "failed",
                    "cancelled",
                }
            return status.status in {"completed", "failed", "cancelled"}

        async for snapshot in self.stream(
                task_id,
                run_id,
                lambda: controller.is_cancelled,
                is_terminal=is_terminal,
        ):
            for mutation in snapshot.mutations:
                self._apply_state_mutation(controller, mutation)

    def _apply_state_mutation(self, controller: Any, mutation: ConversationStateMutation) -> None:
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
