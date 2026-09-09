"""供运行时任务使用的 Agent profile 值对象。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.agents.model_settings import ModelSettings
from app.models import ConversationRunRecord

if TYPE_CHECKING:
    from app.core.tools.schemas.tool_definition import ToolDefinition
    from app.core.workflows.agent_workflow import AgentWorkflow


class AgentProfileType(str, Enum):
    """Agent 分类，决定它在运行时如何被暴露与调度。

    取值：
        MAIN: 主 Agent，全局唯一（系统内置，不对用户开放配置）。
        CHILD: 供主 Agent 委派的子 Agent（出现在委派摘要、可被 delegation 调用）。
        HIDDEN: 隐藏在系统内部的 Agent（如上下文压缩），不向委派摘要暴露、不可被委派。
    """

    MAIN = "main"
    CHILD = "child"
    HIDDEN = "hidden"


def _default_workflow() -> AgentWorkflow:
    """返回默认 ReAct-like 工作流实例（延迟导入，打破循环依赖）。

    把对具体 workflow 实现类的导入推迟到运行时，避免
    ``profile → react → runtime_operations → context → profile`` 的模块级循环导入。
    """

    from app.core.workflows.react import ReactLikeWorkflow

    return ReactLikeWorkflow()


@dataclass
class AgentProfile:
    """描述某个任务的 Agent 执行主体（能力事实源）。
    description 应该是"选择指南"，prompt_file_path 应该是"执行协议"，而本次
    delegate_task.prompt 才是"具体工作单"

    字段：
        agent_id: 持久化在任务和事件上的稳定 Agent 标识。
        role: 人类可读的 Agent 角色。
        description: 子 Agent 的职责/能力/适用场景与约束描述（delegate_task 中暴露给父 Agent）；
            主 Agent 不设置此字段。
        allowed_tools: 该 Agent 允许使用的工具名或权限名。
        workflow: 执行策略（默认 ReAct-like，延迟导入打破循环依赖）。
        provider_id: 模型厂商 id（None 时由 model_name 推导）。
        model_name: 模型名称（可带 provider 前缀）。2026-08-18 决议：内置 profile 不内置
            默认模型，默认 None；None 表示未配置，由前端优先校验、后端兜底报错。
        model_settings: 模型覆盖配置（``ModelSettings``）。
        agent_type: Agent 分类（``AgentProfileType``），决定其在运行时的暴露与调度方式。
        max_steps: 单 run 最大步骤数。
        run: 当前所属 Conversation Run 记录（经 ``derive_for_run`` 注入 per-run 副本；
            单例上不原地写）。
        runtime_event_loop: 运行时事件循环（可为 None）。
        prompt_file_path: 关联的 prompt 文件路径（可为 None）。
    """

    agent_id: str
    role: str
    allowed_tools: list[str]
    agent_type: AgentProfileType
    description: str | None = field(default=None, kw_only=True)
    workflow: AgentWorkflow = field(default_factory=_default_workflow)
    provider_id: int | None = None
    model_name: str | None = None
    model_settings: ModelSettings = field(default_factory=ModelSettings)
    max_steps: int = 1000
    run: ConversationRunRecord | None = None
    runtime_event_loop: asyncio.AbstractEventLoop | None = None
    prompt_file_path: Path | None = None

    def derive_for_run(
        self,
        run: ConversationRunRecord,
        *,
        allowed_tools: list[str] | None = None,
        runtime_event_loop: asyncio.AbstractEventLoop | None = None,
    ) -> AgentProfile:
        """为一次独立的 Conversation Run 执行派生 per-run 副本。

        并发隔离收口：AgentProfile 是注册表共享单例，禁止调用方对其原地写运行时字段
        （并发 run 会互相覆盖）。每次 run 执行必须先经本方法派生独立副本，副本承载
        本次执行的 ``run`` 与可选的 ``runtime_event_loop``（及收窄后的 ``allowed_tools``），
        不同 run 的副本互不串扰。

        Args:
            run: 本次执行的 Conversation Run 记录（必填，写入副本的 ``run`` 字段）。
            allowed_tools: 覆盖工具白名单；None 表示沿用当前值（委派子 Agent 收窄工具集时传入）。
            runtime_event_loop: 覆盖事件广播 loop；None 表示沿用当前值
                （主路径缺省 None；委派 child 传入父 loop，child 事件经
                ``call_soon_threadsafe`` 跨线程投递回父 loop）。

        Returns:
            绑定当前 run 的独立 ``AgentProfile`` 副本（不修改 ``self`` 原实例）。
        """

        changes: dict = {"run": run}
        if allowed_tools is not None:
            changes["allowed_tools"] = allowed_tools
        if runtime_event_loop is not None:
            changes["runtime_event_loop"] = runtime_event_loop
        return replace(self, **changes)

    def select_tools(self, tools: Iterable[ToolDefinition]) -> list[ToolDefinition]:
        """从候选工具中筛选本 Agent 可运行的工具集合。

        工具「能否运行」由 Agent profile 全权决定，调用方（如运行底座）只按 workspace
        可见性给出候选，不再自行做权限门禁，避免职责分散。

        Args:
            tools: 候选工具定义集合（通常按 workspace 可见性预筛后）。

        Returns:
            名称被 ``allowed_tools`` 覆盖的工具定义列表（纯函数，不修改入参）。
        """

        return [tool for tool in tools if tool.name in self.allowed_tools]

    def to_dict(self) -> dict:
        """将 Agent profile 转换为可 JSON 序列化的字典。

        字段集与 ``AgentProfileResponse`` 保持一致；``workflow`` 取 Protocol 声明的
        ``workflow_id``（漏定义在实现侧即类型错误）。
        """

        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "description": self.description,
            "allowed_tools": self.allowed_tools,
            "agent_type": self.agent_type.value,
            "workflow": self.workflow.workflow_id,
            "model_name": self.model_name,
            "max_steps": self.max_steps,
            "prompt_file_path": self.prompt_file_path,
        }
