"""供运行时任务使用的 Agent profile 值对象。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.core.llm.model_settings import ModelSettings

if TYPE_CHECKING:
    from app.core.workflows.agent_workflow import AgentWorkflow
    from app.tools.schemas.tool_definition import ToolDefinition


# 默认 Agent 标识：前端未显式选择 agent 时回落到该内置 developer。
DEFAULT_AGENT_ID = "developer"
DEFAULT_DEVELOPER_TOOLS = (
    "read_file",
    "list_directory",
    "search_files",
    "write_file",
    "patch",
    "delete",
    "execute_terminal",
)


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
        goal: 注入到模型上下文中的运行目标，标明Agent职责。
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
    allowed_tools: list[str]
    context_policy: str
    workflow: AgentWorkflow = field(default_factory=_default_workflow)
    model_name: str = "deepseek-v4-flash"
    model_settings: ModelSettings = field(default_factory=ModelSettings)
    max_steps: int = 1000

    def select_tools(self, tools: Iterable[ToolDefinition]) -> list[ToolDefinition]:
        """从候选工具中筛选本 Agent 可运行的工具集合。

        工具「能否运行」的最终决定由 Agent profile 全权负责，调用方（如运行底座）
        只负责按 workspace 可见性给出候选，不再自行做权限门禁，避免职责分散。

        参数:
            tools: 候选工具定义集合（通常为按 workspace 可见性预筛后的结果）。

        返回:
            仅保留名称或权限被 ``allowed_tools`` 覆盖的工具定义列表。

        异常:
            无。

        副作用:
            无（纯函数，不修改入参）。
        """

        return [tool for tool in tools if tool.name in self.allowed_tools]

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
        role="coding-agent-flush",
        goal="协助用户完成软件工程项目开发任务",
        allowed_tools=list(DEFAULT_DEVELOPER_TOOLS),
        context_policy="text_only_v1",
        model_name="deepseek-v4-flash",
        model_settings=ModelSettings(
            base_url="https://api.deepseek.com",
            api_key_env="DEEPSEEK_API_KEY",
        ),
    )


def developer_agent_pro() -> AgentProfile:
    """构建开发者 Agent profile Pro。

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
        agent_id="developer_pro",
        role="coding-agent-pro",
        goal="协助用户完成软件工程项目开发任务",
        allowed_tools=list(DEFAULT_DEVELOPER_TOOLS),
        context_policy="text_only_v1",
        model_name="deepseek-v4-pro",
        model_settings=ModelSettings(
            base_url="https://api.deepseek.com",
            api_key_env="DEEPSEEK_API_KEY",
        ),
    )
