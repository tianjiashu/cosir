"""供运行时任务使用的 Agent profile 值对象。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.core.agents.prompt_ref import PromptRef
from app.core.llm.model_settings import ModelSettings
from app.models import TurnRecord, TaskRecord

if TYPE_CHECKING:
    from app.core.workflows.agent_workflow import AgentWorkflow
    from app.tools.schemas.tool_definition import ToolDefinition


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


@dataclass
class AgentProfile:
    """描述某个任务的 Agent 执行主体（能力事实源）。

    参数:
        agent_id: 持久化在任务和事件上的稳定 Agent 标识。
        role: 人类可读的 Agent 角色。
        description: 该 Agent 的职责、能力、适用场景与约束的**唯一**文本描述
            （替代旧 ``goal``，并收敛原有 ``capabilities``/``recommended_use_cases``/
            ``constraints`` 等分散字段）。在 delegate_task 中暴露给父 Agent。
        can_delegated: 是否允许被委派为子 Agent。
        allowed_tools: 该 Agent 允许使用的工具名或权限名。
        workflow: 该 Agent 使用的执行策略（默认 ReAct-like）。
        model_name: 该 Agent 使用的模型名称。
        model_settings: 该 Agent 的模型覆盖配置值对象（``ModelSettings``）。
        turn: 该 Agent 当前所属 turn 记录（运行时注入，可为 None）。
        runtime_event_loop: 运行时事件循环（可为 None）。
        prompt_ref: 关联的 prompt 引用（第二部分接缝，第一部分不消费）；可为 None。

    返回:
        不可变的 Agent profile 值对象。

    异常:
        无。

    副作用:
        无。
    """

    agent_id: str
    role: str
    description: str
    allowed_tools: list[str]
    workflow: AgentWorkflow = field(default_factory=_default_workflow)
    model_name: str = "deepseek-v4-flash"
    model_settings: ModelSettings = field(default_factory=ModelSettings)
    max_steps: int = 1000
    turn: TurnRecord | None = None
    main_agent: bool = False
    runtime_event_loop: asyncio.AbstractEventLoop | None = None
    prompt_ref: PromptRef | None = None

    @property
    def can_delegated(self) -> bool:
        """是否允许被委派为子 Agent。

        参数:
            无。

        返回:
            当 ``description`` 非空时返回 ``True``（有可读职责的 Agent 才可被委派）。
            注意：空 ``description`` 的 profile 将被视为不可委派，新增无描述子 agent
            时会因此无法进入 ``child_agent_summary`` 投影。

        异常:
            无。

        副作用:
            无。
        """
        return self.description is not None

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
            包含稳定 Agent profile 字段的字典；字段集与 ``AgentProfileResponse`` 完全一致。

        异常:
            无。

        副作用:
            无。
        """

        prompt_ref_dict = None
        if self.prompt_ref is not None:
            prompt_ref_dict = {
                "name": self.prompt_ref.name,
                "label": self.prompt_ref.label,
                "fallback_path": self.prompt_ref.fallback_path,
                "variables_schema": self.prompt_ref.variables_schema,
            }

        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "description": self.description,
            "allowed_tools": self.allowed_tools,
            "workflow": getattr(self.workflow, "workflow_id", "custom"),
            "model_name": self.model_name,
            "max_steps": self.max_steps,
            "prompt_ref": prompt_ref_dict,
        }
