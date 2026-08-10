"""Synchronous bridge for delegated child Agent runtime execution."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable

from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfile
from app.models.enums.event_type import EventType
from app.models.event.runtime_event import RuntimeEvent
from app.service.delegation.delegation_result import DelegationResult


class ChildAgentRunner:
    """Run a child AgentProfile through the existing AgentRuntime.run_agent entry."""

    def __init__(
        self,
        run_agent: Callable[[AgentProfile], AsyncGenerator[RuntimeEvent, None]],
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

    def run_child(self, child_profile: AgentProfile) -> DelegationResult:
        """同步运行 child Agent 并返回委派终态。

        参数:
            child_profile: 已绑定 child turn 且已收窄工具权限的 AgentProfile。

        返回:
            child 执行的 DelegationResult；如果当前线程已有运行中的事件循环，则返回
            ``status="failed"`` 且 ``error="delegation_runner_event_loop_thread"``。

        异常:
            无。run_agent 内部异常会被转换为 failed DelegationResult。

        副作用:
            通过 ``asyncio.run`` 驱动现有 AgentRuntime.run_agent 异步生成器。
        """

        turn_id = child_profile.turn.turn_id if child_profile.turn is not None else ""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._consume_child_events(child_profile))
        return DelegationResult(
            status="failed",
            child_turn_id=turn_id,
            error="delegation_runner_event_loop_thread",
        )

    async def _consume_child_events(self, child_profile: AgentProfile) -> DelegationResult:
        """消费 child runtime event 流并压缩为委派结果。

        参数:
            child_profile: 已绑定 child turn 且已收窄工具权限的 AgentProfile。

        返回:
            从 child RUN_* 终态事件压缩出的 DelegationResult。

        异常:
            无。run_agent 或事件消费异常会被转换为 failed DelegationResult。

        副作用:
            迭代执行现有 AgentRuntime.run_agent 异步生成器。
        """

        turn_id = child_profile.turn.turn_id if child_profile.turn is not None else ""
        latest_final_text = ""
        try:
            async for event in self._run_agent(child_profile):
                if event.event_type == EventType.FINAL_RESPONSE:
                    latest_final_text = str(getattr(event.payload, "text", "") or "")
                    continue
                if event.event_type == EventType.RUN_FINISHED:
                    return DelegationResult(
                        status="completed",
                        child_turn_id=turn_id,
                        summary=latest_final_text or "child turn completed",
                    )
                if event.event_type == EventType.RUN_FAILED:
                    error = str(
                        getattr(event.payload, "error", None)
                        or getattr(event.payload, "message", None)
                        or "child turn failed"
                    )
                    return DelegationResult(status="failed", child_turn_id=turn_id, error=error)
                if event.event_type == EventType.RUN_CANCELLED:
                    error = str(getattr(event.payload, "error", None) or "child turn cancelled")
                    return DelegationResult(
                        status="cancelled",
                        child_turn_id=turn_id,
                        error=error,
                    )
        except Exception as exc:
            log.exception(
                "delegation_child_run_failed",
                extra={
                    "msg": "委派 child Agent 运行异常，已转换为委派失败结果",
                    "data": {"child_turn_id": turn_id, "error": str(exc)},
                },
            )
            return DelegationResult(
                status="failed",
                child_turn_id=turn_id,
                error=str(exc) or "child turn failed",
            )
        return DelegationResult(
            status="failed",
            child_turn_id=turn_id,
            error="child turn ended without terminal event",
        )
