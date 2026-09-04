"""Synchronous bridge for delegated child Agent runtime execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.assistant_transport.service.conversation_task_snapshot_service import (
    ConversationTaskSnapshotService,
)
from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfile
from app.models.result.delegation_result import DelegationResult
from app.service.depends import get_conversation_run_service


class ChildAgentRunner:
    """Run a child AgentProfile through the existing AgentRuntime.run_agent entry."""

    def __init__(
        self,
        run_agent: Callable[[AgentProfile], Awaitable[None]],
        should_cancel: Callable[[str], bool] | None = None,
        run_executor: object | None = None,
    ) -> None:
        """初始化 child agent 运行桥接器。

        参数:
            run_agent: 现有 AgentRuntime.run_agent 入口。

        返回:
            无。

        异常:
            无。

        副作用:
            保存 run_agent 回调引用。
        """

        self._run_agent = run_agent
        self._should_cancel = should_cancel or (lambda _run_id: False)
        self._run_executor = run_executor

    def run_child(
        self,
        child_profile: AgentProfile,
        delegation_id: str = "",
    ) -> DelegationResult:
        """同步运行 child Agent 并返回委派终态。

        参数:
            child_profile: 已绑定 child run 且已收窄工具权限的 AgentProfile。
            delegation_id: 本次委派在 ``delegations`` 表中的标识，用于异常日志定位
                并发重入场景；无上下文时为 ``""``。

        返回:
            child 执行的 DelegationResult；如果当前线程已有运行中的事件循环（无法再
            用 ``asyncio.run`` 驱动 child），则返回 ``status="failed"`` 且
            ``error="delegation_runner_event_loop_thread"``。

        异常:
            无。run_agent 内部异常或事件循环冲突探测异常均会被转换为 failed
            DelegationResult。

        副作用:
            通过 ``asyncio.run`` 驱动现有 AgentRuntime.run_agent 异步生成器；冲突时
            写入 error 级日志。
        """

        run_id = str(child_profile.run.id) if child_profile.run is not None else ""
        if self._is_running_event_loop_thread():
            log.error(
                "delegation_runner_event_loop_conflict",
                extra={
                    "msg": "委派 child 运行桥接在事件循环线程内被调用，无法启动新 loop",
                    "data": {
                        "child_run_id": run_id,
                        "delegation_id": delegation_id,
                        "error": "delegation_runner_event_loop_thread",
                    },
                },
            )
            return DelegationResult(
                status="failed",
                child_run_id=run_id,
                error="delegation_runner_event_loop_thread",
            )
        return asyncio.run(self._run_child(child_profile, delegation_id))

    @staticmethod
    def _is_running_event_loop_thread() -> bool:
        """判断当前线程是否已有运行中的事件循环。

        采用 ``asyncio.get_running_loop()`` 探测：当前线程存在运行中事件循环时返回
        该 loop，否则抛出 ``RuntimeError``。此处用 try/except 捕获 ``RuntimeError``
        并将其语义唯一地解释为「当前线程无运行中事件循环」，从而返回 ``False``。

        该方式不依赖 ``get_event_loop()``，因此不会在「当前线程无 loop」时抛
        ``RuntimeError`` 破坏调用方主流程，也不会由事件循环策略新建空闲 loop 污染
        当前线程（``asyncio.run`` 自行管理 loop 生命周期）。

        参数:
            无。

        返回:
            当前线程存在运行中事件循环时为 ``True``，否则 ``False``。

        异常:
            无。``get_running_loop`` 抛出的 ``RuntimeError`` 在内部被捕获并归一为
            ``False``。

        副作用:
            无。纯只读探测，不创建或绑定任何事件循环。
        """

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True

    async def _run_child(
        self,
        child_profile: AgentProfile,
        delegation_id: str = "",
    ) -> DelegationResult:
        """运行 child Agent 并从持久化 Conversation Run 事实压缩为委派结果。

        参数:
            child_profile: 已绑定 child run 且已收窄工具权限的 AgentProfile。
            delegation_id: 本次委派标识，透传给异常日志以提升并发重入排查能力。

        返回:
            从 child run 终态事实压缩出的 DelegationResult。

        异常:
            无。run_agent 或事件消费异常会被转换为 failed DelegationResult。

        副作用:
            执行 AgentRuntime.run_agent；不消费运行时事件。
        """

        run_id = str(child_profile.run.id) if child_profile.run is not None else ""
        try:
            if self._should_cancel(run_id):
                return DelegationResult(
                    status="cancelled",
                    child_run_id=run_id,
                    error="child run cancelled",
                )
            executor = self._run_executor
            if executor is None:
                from app.service.depends import get_conversation_run_executor

                executor = get_conversation_run_executor()
            child_run = child_profile.run
            if child_run is None:
                raise RuntimeError("child profile has no conversation run")
            execution = await executor.start(
                child_run.id,
                lambda _run: self._run_agent(child_profile),
            )
            await execution
        except Exception as exc:
            log.exception(
                "delegation_child_run_failed",
                extra={
                    "msg": "委派 child Agent 运行异常，已转换为委派失败结果",
                    "data": {
                        "child_run_id": run_id,
                        "delegation_id": delegation_id,
                        "error": str(exc),
                    },
                },
            )
            return DelegationResult(
                status="failed",
                child_run_id=run_id,
                error=str(exc) or "child run failed",
            )
        if self._should_cancel(run_id):
            return DelegationResult(
                status="cancelled",
                child_run_id=run_id,
                error="child run cancelled",
            )
        child_run = get_conversation_run_service().get_run(int(run_id))
        if child_run.status == "completed":
            state = ConversationTaskSnapshotService().ensure_state_snapshot(child_run.task_id)
            summary = "child run completed"
            for message in state["messages"]:
                if message["role"] == "assistant":
                    summary = (
                        "".join(
                            str(part.get("text", ""))
                            for part in message["parts"]
                            if part.get("type") == "text"
                        )
                        or summary
                    )
            return DelegationResult(
                status="completed",
                child_run_id=run_id,
                summary=summary,
            )
        if child_run.status == "cancelled":
            return DelegationResult(
                status="cancelled", child_run_id=run_id, error="child run cancelled"
            )
        return DelegationResult(
            status="failed",
            child_run_id=run_id,
            error=child_run.end_reason or "child run failed",
        )
