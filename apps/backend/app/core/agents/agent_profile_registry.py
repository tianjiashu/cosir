"""内存中的 agent profile 目录。

单一职责：按 ``agent_id`` 注册 / 解析 / 列举已知 profile。
不持久化（纯内存）、不持有运行态、不负责工具实现。

未来若需持久化或更多 agent，只需把这里从「内存 dict」升级为「DB 加载 + 内存缓存」，
对外接口（``register`` / ``resolve`` / ``list``）保持不变。
"""

from __future__ import annotations

from app.core.agents.agent_profile import AgentProfile


class AgentProfileRegistry:
    """按 ``agent_id`` 注册与解析 ``AgentProfile`` 的内存目录。

    本类是「发现」职责的单一事实来源：profile 自身只描述「agent 是什么」，
    注册表负责「有哪些 agent / 按 id 找得到」，二者的职责互不越界。
    """

    def __init__(self) -> None:
        self._profiles: dict[str, AgentProfile] = {}

    def register(self, profile: AgentProfile) -> None:
        """把一个 agent profile 注册进目录（同 id 覆盖）。

        参数:
            profile: 待注册的不可变 ``AgentProfile``。

        返回:
            无。

        异常:
            无。

        副作用:
            写入或覆盖 ``self._profiles[profile.agent_id]``。
        """

        self._profiles[profile.agent_id] = profile

    def resolve(self, agent_id: str) -> AgentProfile | None:
        """按 ``agent_id`` 解析出对应的 profile。

        参数:
            agent_id: 待解析的 agent 标识。

        返回:
            命中时返回对应的 ``AgentProfile``；未命中返回 ``None``（调用方应据此
            走 ``agent_profile_unavailable`` 失败分支）。

        异常:
            无。

        副作用:
            无。
        """
        if not agent_id and agent_id not in self._profiles:
            return None

        return self._profiles.get(agent_id)

    def list(self) -> list[AgentProfile]:
        """列举当前目录中所有已注册的 profile。

        参数:
            无。

        返回:
            已注册 ``AgentProfile`` 的列表（顺序即插入顺序）。

        异常:
            无。

        副作用:
            无。
        """

        return list(self._profiles.values())

    def list_agent_ids(self) -> set[str]:
        """
        列举当前目录中所有已注册的 agent_id。

        参数:
            无。
        """
        return set(self._profiles.keys())

    def child_agent_summary(self) -> str:
        """把注册表中全部委派子 Agent 投影为面向父 Agent 的能力摘要字符串。

        直接基于 ``AgentProfile`` 的 ``description`` 与 ``allowed_tools`` 渲染可读文本，
        把各子 Agent 摘要拼装为面向父 Agent 的可用目标清单。

        参数:
            无（方法消费实例自身的 ``list`` 接口返回全部已注册 profile）。

        返回:
            形如 ``"Available child agents:\\n- agent_id (role): description | tools: ...\\n..."``
            的摘要文本；若注册表中无任何委派子 Agent，返回空字符串。

        异常:
            无（纯函数，只读 registry，不抛预期异常）。

        副作用:
            无（不修改 registry，不写入任何外部状态）。
        """

        blocks: list[str] = []
        for profile in self.list():
            if not profile.can_delegated:
                continue
            tool_capability_summary = "tools: " + ", ".join(profile.allowed_tools)
            blocks.append(
                f"agent_id: {profile.agent_id} ==> role: {profile.role} ==> "
                f"description: {profile.description} | {tool_capability_summary}"
            )

        if not blocks:
            return ""
        return "Available child agents:\n" + "\n".join(blocks)

    def child_agent_ids(self) -> set[str]:
        """
        列举当前目录中所有已注册的委派子 Agent 的 agent_id。

        参数:
            无。
        """
        return {profile.agent_id for profile in self._profiles.values() if profile.can_delegated}
