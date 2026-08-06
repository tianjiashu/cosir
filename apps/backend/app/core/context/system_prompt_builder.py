"""构建面向模型的中文系统提示词。"""

from app.core.agents.agent_profile import AgentProfile
from app.core.context.system_prompt_context import SystemPromptContext
from app.utils.file_utils import read_text_file


class SystemPromptBuilder:
    """按固定分层构建本地 coding-agent 的系统提示词。"""

    system_prompt_context: SystemPromptContext
    agent_profile: AgentProfile

    def build(self, agent_profile: AgentProfile, context: SystemPromptContext) -> str:
        """构建完整系统提示词文本。

        参数:
            agent_profile: 当前执行主体的 Agent 档案。
            context: 当前轮次的运行时上下文事实。

        返回:
            由固定 section 顺序拼接出的中文系统提示词。

        异常:
            无。

        副作用:
            无。
        """
        self.system_prompt_context = context
        self.agent_profile = agent_profile

        sections = [
            self._agent_identity(agent_profile, context),
            self._engineering_principles(),
            self._workflow_contract(),
            self._tool_use_policy(agent_profile),
        ]
        return "\n\n".join(sections)

    def _agent_identity(self, agent_profile: AgentProfile, context: SystemPromptContext) -> str:
        """构建 Agent 身份 section。

        参数:
            agent_profile: 当前执行主体的 Agent 档案。
            context: 当前轮次的运行时上下文事实。

        返回:
            描述身份和基础运行环境的 section 文本。

        异常:
            无。

        副作用:
            无。
        """

        return "\n".join(
            [
                "<agent_identity>",
                f"你是一个运行在用户本机的 {agent_profile.role}，主要职责是{agent_profile.goal}。",
                f"Agent ID: {agent_profile.agent_id}",
                f"Role: {agent_profile.role}",
                f"当前所处于的操作系统: {context.os_name}",
                (
                    f"当前工作区根目录: {context.workspace_root},你所有的代码都在这个目录下，"
                    f"且写、编辑、删除操作将被系统限制在这个目录下，"
                    f"超出这个目录范围的操作将被系统拒绝"
                ),
                f"今天日期: {context.today}",
                f"所拥有的工具集合: {', '.join(agent_profile.allowed_tools) or 'none'}",
                (
                    f"你所面向的用户所使用的语言: {context.language},"
                    f"请使用友好的语言和用户交流。除非用户要求，不要使用emjio表情回复。"
                ),
                "</agent_identity>",
            ]
        )

    def _engineering_principles(self) -> str:
        """构建整洁代码开发原则 section。

        从 ``coding_rule_dir`` 指向的文件读取内容；文件不存在时返回兜底占位文本。

        参数:
            无。

        返回:
            描述代码质量原则的 section 文本。

        异常:
            无（所有异常均在内部兜底处理）。

        副作用:
            无。
        """

        return "\n".join(
            [
                "<engineering_principles>",
                read_text_file(self.system_prompt_context.coding_rule_dir),
                "</engineering_principles>",
            ]
        )

    def _workflow_contract(self) -> str:
        """构建工作流契约 section。

        参数:
            无。

        返回:
            描述执行步骤约束的 section 文本。

        异常:
            无。

        副作用:
            无。
        """

        return "\n".join(
            [
                "<workflow_contract>",
                "- 先明确任务目标和影响范围，再定位相关代码和测试。",
                "- 需要理解代码时，优先搜索和阅读已有实现，再决定修改方案。",
                "- 修改应保持局部、可回滚、可解释，并避免牵连无关文件。",
                "- 完成代码改动后，尽可能运行相关测试、lint 或类型检查形成闭环。",
                "- 无法验证时必须明确说明原因、已完成内容和剩余风险。",
                "</workflow_contract>",
            ]
        )

    def _tool_use_policy(self, agent_profile: AgentProfile) -> str:
        """构建工具使用策略 section。

        参数:
            agent_profile: 当前执行主体的 Agent 档案。

        返回:
            描述允许工具和调用原则的 section 文本。

        异常:
            无。

        副作用:
            无。
        """

        allowed_tools = ", ".join(agent_profile.allowed_tools) or "none"
        return "\n".join(
            [
                "<tool_use_policy>",
                f"Allowed tools: {allowed_tools}",
                "- 工具 schema 是参数结构的唯一事实来源；调用工具时必须遵守 schema。",
                "- 文件读写、搜索、补丁和终端执行应服务于当前任务目标，不做无关探索。",
                "- 代码探索、搜索代码，codegraph工具优先级高于搜索工具",
                "- 写入、删除、补丁和命令执行属于高影响操作，必须基于已确认路径和明确目的。",
                "- 工具失败时先诊断原因并调整路线，不要重复提交同一类无效调用。",
                "</tool_use_policy>",
            ]
        )
