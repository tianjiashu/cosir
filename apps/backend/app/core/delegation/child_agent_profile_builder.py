"""Build per-turn AgentProfile copies for delegated child runs."""

from collections.abc import Iterable
from dataclasses import replace

from app.core.agents.agent_profile import AgentProfile
from app.models.turn_record import TurnRecord


class ChildAgentProfileBuilder:
    """Derive a child run profile without changing the registered profile."""

    @staticmethod
    def build(
        registry_profile: AgentProfile,
        turn: TurnRecord,
        effective_tools: Iterable[str],
    ) -> AgentProfile:
        """Create a profile bound to one child turn and its effective tool set.

        参数:
            registry_profile: 从 AgentProfileRegistry 解析的共享内置 profile。
            turn: 当前 child run 对应的轮次记录。
            effective_tools: 已由父级委派边界收敛后的工具名称。

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
        )
