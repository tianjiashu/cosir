"""工具执行持久化和策略服务。"""

import logging
from typing import Any, Dict, Optional

from app.tool_execution.policy import ToolExecutionPolicy
from app.tool_execution.policy_provider import ToolPolicyContext
from app.tool_execution.lifecycle import ToolExecutionLifecycle
from app.tool_execution.records import ToolCallRecord, ToolPolicyDecision
from app.tool_execution.store import ToolExecutionStore


class ToolExecutionService:
    """创建工具调用记录并执行策略评估。"""

    def __init__(
        self,
        store: ToolExecutionStore,
        policy: ToolExecutionPolicy,
        logger: logging.Logger,
        lifecycle: Optional[ToolExecutionLifecycle] = None,
    ) -> None:
        """初始化工具执行服务。

        参数:
            store: 工具执行仓储。
            policy: 工具执行策略编排器。
            logger: 日志器。
            lifecycle: 可选工具执行生命周期状态机。

        返回:
            无。

        异常:
            无。

        副作用:
            保存依赖项。
        """

        self._store = store
        self._policy = policy
        self._logger = logger
        self._lifecycle = lifecycle or ToolExecutionLifecycle()

    def plan_tool_call(
        self,
        run_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        permission: str,
        idempotency_key: str,
        step_id: Optional[str] = None,
    ) -> tuple[ToolCallRecord, ToolPolicyDecision]:
        """创建工具调用记录并返回策略决策。

        参数:
            run_id: 所属运行标识符。
            tool_name: 工具名称。
            arguments: 工具参数。
            permission: 权限级别。
            idempotency_key: 幂等键。
            step_id: 可选步骤标识符。

        返回:
            工具调用记录和策略决策。

        异常:
            sqlite3.Error: 如果持久化失败。

        副作用:
            写入工具调用记录并记录策略日志。
        """

        existing = self._store.get_tool_call_by_key(idempotency_key)
        call = self._store.create_tool_call(
            run_id=run_id,
            tool_name=tool_name,
            arguments=arguments,
            permission=permission,
            idempotency_key=idempotency_key,
            step_id=step_id,
        )
        decision = self._policy.decide(
            ToolPolicyContext(
                tool_name=tool_name,
                arguments=arguments,
                permission=permission,
                run_id=run_id,
            )
        )
        if existing is not None:
            self._logger.info(
                "tool_policy_reused run_id=%s tool_call_id=%s tool=%s status=%s",
                run_id,
                existing.tool_call_id,
                tool_name,
                existing.status,
            )
            return existing, decision
        planned_status = self._lifecycle.status_for_policy(decision)
        call = self._update_status(call, planned_status)
        self._logger.info(
            "tool_policy_decided run_id=%s tool_call_id=%s tool=%s status=%s risk=%s",
            run_id,
            call.tool_call_id,
            tool_name,
            decision.status,
            decision.risk_level,
        )
        return call, decision

    def get_call_by_key(self, idempotency_key: str) -> Optional[ToolCallRecord]:
        """按幂等键查询已有工具调用事实。

        参数:
            idempotency_key: 稳定工具调用幂等键。

        返回:
            存在时返回工具调用记录，否则返回 None。

        异常:
            sqlite3.Error: 如果查询失败。

        副作用:
            无。
        """

        return self._store.get_tool_call_by_key(idempotency_key)

    def mark_running(self, tool_call_id: str) -> ToolCallRecord:
        """将已批准的工具调用标记为运行中。

        参数:
            tool_call_id: 工具调用标识。

        返回:
            更新后的工具调用记录。

        异常:
            KeyError: 如果工具调用不存在。
            ValueError: 如果当前状态不能进入 running。

        副作用:
            更新 tool_calls 表。
        """

        return self._update_status(self._store.get_tool_call(tool_call_id), "running")

    def mark_approved(self, tool_call_id: str) -> ToolCallRecord:
        """将等待审批的工具调用标记为已批准。

        参数:
            tool_call_id: 工具调用标识。

        返回:
            更新后的工具调用记录。

        异常:
            KeyError: 如果工具调用不存在。
            ValueError: 如果当前状态不能进入 approved。

        副作用:
            更新 tool_calls 表。
        """

        return self._update_status(self._store.get_tool_call(tool_call_id), "approved")

    def mark_completed(self, tool_call_id: str) -> ToolCallRecord:
        """将运行中的工具调用标记为完成。

        参数:
            tool_call_id: 工具调用标识。

        返回:
            更新后的工具调用记录。

        异常:
            KeyError: 如果工具调用不存在。
            ValueError: 如果当前状态不能进入 completed。

        副作用:
            更新 tool_calls 表。
        """

        return self._update_status(self._store.get_tool_call(tool_call_id), "completed")

    def mark_failed(self, tool_call_id: str) -> ToolCallRecord:
        """将运行中的工具调用标记为失败。

        参数:
            tool_call_id: 工具调用标识。

        返回:
            更新后的工具调用记录。

        异常:
            KeyError: 如果工具调用不存在。
            ValueError: 如果当前状态不能进入 failed。

        副作用:
            更新 tool_calls 表。
        """

        return self._update_status(self._store.get_tool_call(tool_call_id), "failed")

    def mark_cancelled(self, tool_call_id: str) -> ToolCallRecord:
        """将未执行副作用的工具调用标记为取消。

        参数:
            tool_call_id: 工具调用标识。

        返回:
            更新后的工具调用记录。

        异常:
            KeyError: 如果工具调用不存在。
            ValueError: 如果当前状态不能进入 cancelled。

        副作用:
            更新 tool_calls 表。
        """

        return self._update_status(self._store.get_tool_call(tool_call_id), "cancelled")

    def create_execution(
        self,
        tool_call_id: str,
        status: str,
        effect_status: str,
        artifact_id: Optional[str] = None,
        error: Optional[str] = None,
    ):
        """创建单次 handler 执行事实记录。

        参数:
            tool_call_id: 所属工具调用标识。
            status: handler 执行状态。
            effect_status: 副作用状态。
            artifact_id: 可选 artifact 标识。
            error: 可选错误文本。

        返回:
            新建的工具执行记录。

        异常:
            KeyError: 如果关联工具调用不存在。
            sqlite3.Error: 如果记录无法写入。

        副作用:
            写入 tool_executions 表。
        """

        return self._store.create_execution(
            tool_call_id=tool_call_id,
            status=status,
            effect_status=effect_status,
            artifact_id=artifact_id,
            error=error,
        )

    def _update_status(self, call: ToolCallRecord, target: str) -> ToolCallRecord:
        """校验并写入一次工具调用状态迁移。

        参数:
            call: 当前工具调用记录。
            target: 目标生命周期状态。

        返回:
            更新后的工具调用记录。

        异常:
            ValueError: 如果状态迁移非法。
            sqlite3.Error: 如果状态无法写入。

        副作用:
            更新 tool_calls 表。
        """

        self._lifecycle.ensure_transition(call.status, target)
        return self._store.update_tool_call_status(call.tool_call_id, target)
