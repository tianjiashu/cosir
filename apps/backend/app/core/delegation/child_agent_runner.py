"""Synchronous bridge for delegated child Agent runtime execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfile
from app.models.result.delegation_result import DelegationResult
from app.service.depends import (
    get_conversation_run_executor,
    get_conversation_run_state_service,
)


class ChildAgentRunner:
    """Run a child AgentProfile through the existing AgentRuntime.run_agent entry."""

    def __init__(
        self,
        run_agent: Callable[..., Awaitable[None]],
        should_cancel: Callable[[int], bool] | None = None,
    ) -> None:
        """初始化 child agent 运行桥接器。

        参数:
            run_agent: 现有 AgentRuntime.run_agent 入口。
            should_cancel: 可选的按 run 查询取消状态的回调。

        返回:
            无。

        异常:
            无。

        副作用:
            保存 run_agent 回调引用。
        """

        self._run_agent = run_agent
        self._should_cancel = should_cancel or (lambda _run_id: False)
        self._run_executor = get_conversation_run_executor()

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

        run_id = child_profile.run.id
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

    async def run_child_workflow(self, child_profile: AgentProfile) -> None:
        """Run an already-claimed child workflow on the backend event loop.

        ``ChildAgentSessionService`` owns pending→running claiming and
        ``ConversationRunExecutor`` registration.  This entry point therefore only
        executes the supplied per-run profile and never calls ``asyncio.run``.
        """

        await self._run_agent(child_profile)

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
            从 child run 终态事实压缩出的 DelegationResult；completed/cancelled/failed 三类
            终态均会携带 run 的 final_output 作为 summary，供主 Agent 感知子 Agent 的产出或
            终态原因（委派场景中子 Agent 复用同一工作流，缺少 final_output 主 Agent 无法判断结果）。

        异常:
            无。认领失败（child run 非 ``pending``）、run_agent 或事件消费异常都会被转换为
            failed DelegationResult。

        副作用:
            先把 child run 认领为 ``running``（``claim_pending_run``，成功时发布一次 RUNNING
            状态事件，该认领是 ``ConversationRunExecutor.start`` 前置断言的必要条件）；
            随后执行 AgentRuntime.run_agent；不消费运行时事件；必要时经
            _build_cancelled_result / _build_failed_result 原子收口未终态化的 run 并写入
            final_output。
        """

        run_id = child_profile.run.id
        try:
            if self._should_cancel(run_id):
                return self._build_cancelled_result(run_id)
            child_run = child_profile.run
            if child_run is None:
                raise RuntimeError("child profile has no conversation run")
            # 认领 child run（pending→running）。主链路在 ConversationRunCommandService
            # 的 new/edit 阶段认领，而本桥接器是 child run 的唯一启动入口：``create_run``
            # 建成的是 pending，而 ``ConversationRunExecutor.start`` 只接受 running，
            # 因此认领必须发生在其前置断言之前（否则每次委派都以「run N is not running」
            # 失败——2026-09-17 线上缺陷）。
            claimed_run = get_conversation_run_state_service().claim_pending_run(child_run.id)
            if claimed_run is None:
                raise RuntimeError(
                    f"child run {child_run.id} is not claimable: its status is not pending"
                )
            execution = await self._run_executor.start(
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
            return self._build_failed_result(run_id, str(exc) or "child run failed")
        if self._should_cancel(run_id):
            return DelegationResult(
                status="cancelled",
                child_run_id=run_id,
                error="child run cancelled",
            )
        child_run = get_conversation_run_state_service().get_run(run_id)
        if child_run.status == "completed":
            return DelegationResult(
                status="completed",
                child_run_id=run_id,
                summary=child_run.final_output,
            )
        if child_run.status == "cancelled":
            return DelegationResult(
                status="cancelled",
                child_run_id=run_id,
                error="child run cancelled",
                summary=child_run.final_output,
            )
        return DelegationResult(
            status="failed",
            child_run_id=run_id,
            error=child_run.end_reason or "child run failed",
            summary=child_run.final_output,
        )

    def _build_cancelled_result(self, run_id: int) -> DelegationResult:
        """构造 cancelled 终态的委派结果，并确保 child run 已落定终态且携带 final_output。

        当 child 工作流尚未启动即被取消、或主 Agent 在流式中途取消时，run 可能停留在
        running/pending 未被收口，本方法负责原子收口为 cancelled 并写入可让主 Agent 感知的
        final_output；若工作流已先落定终态（携带部分输出），则保留既有 final_output 不再覆盖。

        参数:
            run_id: child run 标识。

        返回:
            status="cancelled" 的 DelegationResult，其 summary 携带 run 的 final_output。
        """
        service = get_conversation_run_state_service()
        run = service.cancel_run_if_running(
            run_id,
            end_reason="runtime_cancelled",
            final_output="child run cancelled before execution",
        )
        if run is None:
            run = service.get_run(run_id)
        return DelegationResult(
            status="cancelled",
            child_run_id=run_id,
            error="child run cancelled",
            summary=run.final_output,
        )

    def _build_failed_result(self, run_id: int, error: str) -> DelegationResult:
        """构造 failed 终态的委派结果，并确保 child run 已落定终态且携带 final_output。

        异常路径下 run 可能停留在 running/pending 未被收口，本方法负责原子收口为 failed 并写入
        可让主 Agent 感知的 final_output（异常原因）；若工作流已先落定终态，则保留既有
        final_output 不再覆盖。

        参数:
            run_id: child run 标识。
            error: 失败原因文本。

        返回:
            status="failed" 的 DelegationResult，其 summary 携带 run 的 final_output。
        """
        service = get_conversation_run_state_service()
        run = service.fail_run_if_running(run_id, end_reason=error, final_output=error)
        if run is None:
            run = service.get_run(run_id)
        return DelegationResult(
            status="failed",
            child_run_id=run_id,
            error=error,
            summary=run.final_output,
        )
