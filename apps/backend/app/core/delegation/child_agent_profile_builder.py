"""为委派 child run 构建单次 AgentProfile 副本。"""

from collections.abc import Iterable
from dataclasses import replace

from app.core.agents.agent_profile import AgentProfile
from app.models.turn_record import TurnRecord


class ChildAgentProfileBuilder:
    """从注册表 profile 派生 child run profile，不修改共享 profile。"""

    @staticmethod
    def build(
        registry_profile: AgentProfile,
        turn: TurnRecord,
        effective_tools: Iterable[str],
        context_excluded_turn_ids: Iterable[str] = (),
        runtime_event_loop=None,
    ) -> AgentProfile:
        """构建绑定单次 child turn 与有效工具集的 profile。

        参数:
            registry_profile: 从 AgentProfileRegistry 解析的共享内置 profile。
            turn: 当前 child run 对应的轮次记录。
            effective_tools: 已由父级委派边界收窄后的工具名称。
            context_excluded_turn_ids: child runtime 加载 task 历史时需要排除的 turn 标识。
            runtime_event_loop: child runtime 事件需要投递回的父运行事件循环。

        返回:
            独立的、绑定当前 turn 的 AgentProfile 派生副本。

        异常:
            无。

        副作用:
            无；不修改 registry_profile 或其 allowed_tools。
        """

        return replace(
            registry_profile,
            turn=turn,
            allowed_tools=list(effective_tools),
            context_excluded_turn_ids=tuple(context_excluded_turn_ids),
            runtime_event_loop=runtime_event_loop,
        )
