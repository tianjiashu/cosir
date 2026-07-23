"""供运行时任务使用的 Agent profile 值对象。"""

from __future__ import annotations

# 默认 Agent 标识：前端未显式选择 agent 时回落到该内置 developer。
DEFAULT_AGENT_ID = "developer"

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List

from app.core.llm.model_settings import ModelSettings

if TYPE_CHECKING:
    from app.core.workflows.agent_workflow import AgentWorkflow


def _default_workflow() -> AgentWorkflow:
    """返回默认 ReAct-like 工作流实例（延迟导入，打破循环依赖）。

    单一职责：仅在构造默认 ``AgentProfile`` 时提供工作流实例，把对具体 workflow 实现类的
    导入推迟到运行时，避免 ``profile → react → runtime_operations → context → profile`` 的
    模块级循环导入。

    参数:
        无。

    返回:
        一个 ``ReactLikeWorkflow`` 实例。

    异常:
        无。

    副作用:
        首次调用时导入 ``app.core.workflows.react``（仅一次）。
    """

    from app.core.workflows.react import ReactLikeWorkflow

    return ReactLikeWorkflow()


@dataclass(frozen=True)
class AgentProfile:
    """描述某个任务的 Agent 执行主体。

    参数:
        agent_id: 持久化在任务和事件上的稳定 Agent 标识。
        role: 人类可读的 Agent 角色。
        goal: 注入到模型上下文中的运行目标。
        allowed_tools: 该 Agent 允许使用的工具名或权限名。
        context_policy: 该 Agent 的上下文处理策略名称。
        workflow: 该 Agent 使用的执行策略（默认 ReAct-like）。
        model_name: 该 Agent 使用的模型名称。
        model_settings: 该 Agent 的模型覆盖配置值对象（``ModelSettings``）。

    返回:
        不可变的 Agent profile 值对象。

    异常:
        无。

    副作用:
        无。
    """

    agent_id: str
    role: str
    goal: str
    allowed_tools: List[str]
    context_policy: str
    workflow: AgentWorkflow = field(default_factory=_default_workflow)
    model_name: str = "deepseek-v4-flash"
    model_settings: ModelSettings = field(default_factory=ModelSettings)
    max_steps: int = 1000

    def allows_tool(self, tool_name: str, permission: str) -> bool:
        """返回该 Agent profile 是否允许某个工具。

        参数:
            tool_name: 已注册的稳定工具名。
            permission: 该工具所需的权限级别。

        返回:
            当 profile 允许该工具名或该权限名时返回 True。

        异常:
            无。

        副作用:
            无。
        """

        allowed = set(self.allowed_tools)
        return tool_name in allowed or permission in allowed

    def to_dict(self) -> dict:
        """将 Agent profile 转换为可 JSON 序列化的字典。

        参数:
            无。

        返回:
            包含稳定 Agent profile 字段的字典。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "goal": self.goal,
            "allowed_tools": self.allowed_tools,
            "context_policy": self.context_policy,
            "workflow": getattr(self.workflow, "workflow_id", "custom"),
            "model_name": self.model_name,
            "max_steps": self.max_steps,
        }


def default_developer_agent() -> AgentProfile:
    """构建第一版默认的开发者 Agent profile。

    参数:
        无。

    返回:
        用于内置单 Agent 的 AgentProfile。

    异常:
        无。

    副作用:
        无。
    """

    return AgentProfile(
        agent_id=DEFAULT_AGENT_ID,
        role="developer",
        goal=(
            "完成本地 coding-agent 任务；优先保持代码清晰、可诊断、可扩展，"
            "第一版只处理纯文本输入。"
        ),
        allowed_tools=["read_file"],
        context_policy="text_only_v1",
        model_name="deepseek-v4-flash",
        model_settings=ModelSettings(
            base_url="https://api.deepseek.com",
            api_key_env="sk-e920522a28a844c2be0d4581f4d9c650",
        ),
    )
